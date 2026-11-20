"""Atomic business commands. Browser input never supplies the acting identity."""
import uuid
from datetime import timedelta
from decimal import Decimal
from django.db import transaction
from django.db.models import F
from django.core.exceptions import ValidationError, PermissionDenied
from django.contrib.auth.models import User
from django.utils import timezone
from .models import Asset, Need, Hold, Decision, Service, Disposition, Impact, Event, Notice
from .policy import require, profile, owns_need

ACTIVE=['Pending Approval','Allocated','Released','Exception']
def audit(user,action,asset=None,reference='',**detail):
    return Event.objects.create(actor=user,action=action,asset=asset,reference=str(reference),detail=detail)
def notify(user,message): Notice.objects.create(user=user,message=message[:250])
def valid_evidence(value):
    if len(value.strip())<8: raise ValidationError('Provide meaningful evidence or a reference (at least 8 characters).')
def version(obj,expected):
    if obj.version!=int(expected): raise ValidationError('STALE_VERSION: reload and review the current record.')
def expire(asset=None):
    qs=Hold.objects.select_for_update().filter(state='Pending Approval',expires_at__lte=timezone.now())
    if asset: qs=qs.filter(asset=asset)
    for h in qs:
        h.state='Expired'; h.save(update_fields=['state'])
        audit(h.proposer,'Hold expired',h.asset,h.pk)
        notify(h.proposer,f'Hold expired for {h.need.title}. Your need remains open.')

def reserve(user,need_id,asset_id,qty,approver_id,receiver_id,cost,key):
    require(user,'Requester','Department Representative')
    qty=int(qty); cost=Decimal(cost)
    if qty<1 or not cost.is_finite() or cost<0: raise ValidationError('Quantity must be positive and cost must be nonnegative.')
    with transaction.atomic():
        # Lock stock first consistently across every command to avoid lock inversion.
        a=Asset.objects.select_for_update().get(pk=asset_id)
        n=Need.objects.select_for_update().get(pk=need_id)
        if not owns_need(user,n): raise PermissionDenied
        existing=Hold.objects.filter(idempotency_key=key).first()
        if existing:
            if (existing.proposer_id,existing.need_id,existing.asset_id,existing.quantity,existing.approver_id,existing.receiver_id,existing.cost)!=(user.pk,n.pk,a.pk,qty,int(approver_id),int(receiver_id),cost):
                raise ValidationError('IDEMPOTENCY_CONFLICT: use a fresh submission key.')
            return existing
        expire(a)
        from .intelligence import eligible
        ok,reasons=eligible(a,n)
        if not ok: raise ValidationError('; '.join(reasons))
        committed=sum(h.quantity for h in n.holds.filter(state__in=ACTIVE))
        if n.state!='Open' or qty>n.quantity-n.received-committed or qty>a.free_quantity:
            raise ValidationError('STOCK_CONFLICT: quantity exceeds free stock or remaining need.')
        if not n.allow_partial and qty!=n.quantity-n.received-committed:
            raise ValidationError('This request does not allow partial fulfillment.')
        approver=User.objects.get(pk=approver_id)
        require(approver,'Assigned Approver')
        if approver==user: raise ValidationError('SELF_APPROVAL: choose another approver.')
        receiver=User.objects.get(pk=receiver_id)
        require(receiver,'Asset Custodian',department=n.department_id)
        h=Hold.objects.create(need=n,asset=a,quantity=qty,proposer=user,approver=approver,receiver=receiver,source_custodian=a.custodian,asset_version=a.version,cost=cost,idempotency_key=key,expires_at=timezone.now()+timedelta(hours=48))
        # Compare-and-swap writes serialize even the local SQLite demo.
        if not Asset.objects.filter(pk=a.pk,version=a.version).update(version=F('version')+1):
            raise ValidationError('STOCK_CONFLICT: retry with current stock.')
        h.asset_version=a.version+1; h.save(update_fields=['asset_version'])
        audit(user,'Reservation created',a,h.pk,quantity=qty,expires=h.expires_at.isoformat())
        notify(a.custodian,f'Safety review required: {n.title}')
        notify(approver,f'Approval assigned: {n.title}')
        return h

def hold_command(user,hold_id,action,expected,evidence='',actual_qty=None,actual_condition=None):
    with transaction.atomic():
        ref=Hold.objects.get(pk=hold_id)
        a=Asset.objects.select_for_update().get(pk=ref.asset_id)
        n=Need.objects.select_for_update().get(pk=ref.need_id)
        h=Hold.objects.select_for_update().get(pk=hold_id)
        expire(a); h.refresh_from_db()
        if action=='receive' and h.state=='Completed':
            require(user,'Asset Custodian',department=n.department_id)
            if h.receiver_id!=user.pk: raise PermissionDenied
            return h
        version(h,expected)
        if action=='safety':
            require(user,'Asset Custodian',department=a.department_id)
            if h.source_custodian_id!=user.pk: raise PermissionDenied
            if h.state!='Pending Approval' or a.status!='Available': raise ValidationError('The asset is restricted or this hold is no longer pending.')
            valid_evidence(evidence); h.safety_review=True
        elif action=='cost':
            require(user,'Finance Officer')
            if h.state!='Pending Approval': raise ValidationError('Cost review requires a pending proposal.')
            valid_evidence(evidence); h.cost_review=True
        elif action in ['approve','reject','return']:
            require(user,'Assigned Approver')
            if h.approver_id!=user.pk: raise PermissionDenied
            if h.proposer_id==user.pk: raise ValidationError('SELF_APPROVAL is prohibited.')
            if h.state!='Pending Approval': raise ValidationError('This proposal is no longer pending.')
            valid_evidence(evidence)
            if action=='approve':
                if not h.safety_review or (h.cost>0 and not h.cost_review): raise ValidationError('MISSING_REVIEW: safety and applicable finance review must be complete.')
                if a.status!='Available': raise ValidationError('Restricted assets cannot be approved for reuse.')
                h.state='Allocated'
            else: h.state='Rejected' if action=='reject' else 'Returned'
            h.reason=evidence
        elif action=='cancel':
            if not owns_need(user,n): raise PermissionDenied
            if h.state!='Pending Approval': raise ValidationError('Only a pending hold can be withdrawn; allocated stock requires approver cancellation.')
            h.state='Cancelled'; evidence='Requester withdrew pending hold'
        elif action=='cancel_approved':
            require(user,'Assigned Approver')
            if h.approver_id!=user.pk or h.state!='Allocated': raise PermissionDenied
            valid_evidence(evidence); h.state='Cancelled'
        elif action=='release':
            require(user,'Asset Custodian',department=a.department_id)
            if h.source_custodian_id!=user.pk: raise PermissionDenied
            if h.state!='Allocated' or a.status!='Available' or not h.safety_review: raise ValidationError('Release requires current allocation and safe stock.')
            valid_evidence(evidence); h.release_evidence=evidence; h.state='Released'
        elif action=='receive':
            require(user,'Asset Custodian',department=n.department_id)
            if h.receiver_id!=user.pk: raise PermissionDenied
            if h.state!='Released': raise ValidationError('Receipt requires a recorded release.')
            valid_evidence(evidence)
            if int(actual_qty)!=h.quantity or actual_condition!=a.condition:
                h.state='Exception'; h.reason='Receipt quantity or condition differs from the approved release. '+evidence
            else:
                if a.quantity<h.quantity: raise ValidationError('Stock reconciliation required.')
                root=a
                while root.parent_id: root=root.parent
                h.completed_root=root.pk
                h.completed_start=a.unit_start+a.quantity-h.quantity
                # Unit ranges remain stable across subsequent stock splits.
                if a.quantity==h.quantity:
                    a.department=n.department; a.custodian=h.receiver; a.campus=n.campus
                    a.building=n.building; a.room=n.room; a.published=False; a.version+=1; a.save()
                    dest=a
                else:
                    a.quantity-=h.quantity; a.version+=1; a.save(update_fields=['quantity','version'])
                    dest=Asset.objects.create(code=f'{a.code[:12]}-T{str(h.id)[:6]}',name=a.name,description=a.description,category=a.category,tags=a.tags,quantity=h.quantity,department=n.department,custodian=h.receiver,campus=n.campus,building=n.building,room=n.room,purchase_date=a.purchase_date,value=a.value,condition=a.condition,published=False,ready_date=timezone.localdate(),parent=a,unwanted=False,unit_start=h.completed_start)
                h.result_asset=dest; h.state='Completed'; h.completed_at=timezone.now(); h.receipt_evidence=evidence
                n.received+=h.quantity
                n.state='Fulfilled' if n.received==n.quantity else 'Open'; n.version+=1; n.save()
                audit(user,'Custody received',dest,h.pk,quantity=h.quantity,source=str(a.pk))
        elif action=='resolve':
            require(user,'Assigned Approver')
            if h.approver_id!=user.pk or h.state!='Exception': raise PermissionDenied
            valid_evidence(evidence)
            # A reconciled receipt remains the receiver's responsibility.
            h.state='Released'; h.reason=evidence
        else: raise ValidationError('Unsupported workflow action.')
        h.version+=1; h.save()
        Decision.objects.create(hold=h,actor=user,version=int(expected),decision=action,reason=evidence)
        audit(user,f'Transfer {action}',a,h.pk,state=h.state,version=h.version,evidence=evidence)
        for recipient in {h.proposer,h.approver,h.source_custodian,h.receiver}: notify(recipient,f'{n.title}: {h.state} ({action})')
        return h

def service_command(user,service_id,action,data):
    with transaction.atomic():
        ref=Service.objects.get(pk=service_id)
        a=Asset.objects.select_for_update().get(pk=ref.asset_id)
        s=Service.objects.select_for_update().get(pk=service_id)
        if action=='work':
            require(user,'Maintenance Staff')
            if s.state!='Inspection required': raise ValidationError('Work has already been recorded.')
            valid_evidence(data['findings']); valid_evidence(data['work'])
            cost=Decimal(data['cost'])
            if not cost.is_finite() or cost<0: raise ValidationError('Invalid service cost.')
            # Cost-bearing repair needs explicit finance/approver stages: baseline supports inspection/no-cost repair.
            if cost>0: raise ValidationError('Cost-bearing repairs require a separate authorized work order; this prototype records no-cost service only.')
            s.findings=data['findings']; s.work=data['work']; s.cost=cost; s.condition_after=data['condition_after']; s.staff=user; s.state='Awaiting verification'
        elif action=='verify':
            require(user,'Asset Custodian',department=a.department_id)
            if a.custodian_id!=user.pk or s.state!='Awaiting verification' or s.staff_id==user.pk: raise PermissionDenied
            valid_evidence(data['evidence'])
            s.evidence=data['evidence']; s.verifier=user
            if data.get('safe')=='yes':
                a.status='Available'; a.condition=s.condition_after; s.state='Safe return verified'; s.completed_at=timezone.now()
            else: s.state='Inspection required'
            a.version+=1; a.save()
        else: raise ValidationError('Invalid service command.')
        s.save(); audit(user,'Maintenance '+action,a,s.pk,state=s.state)
        notify(a.custodian,f'{a.code}: {s.state}')
        return s

def disposition_command(user,pk,action,evidence):
    with transaction.atomic():
        d=Disposition.objects.select_for_update().select_related('asset').get(pk=pk)
        a=Asset.objects.select_for_update().get(pk=d.asset_id)
        valid_evidence(evidence)
        if a.holds.filter(state__in=ACTIVE).exists(): raise ValidationError('Resolve all stock commitments before disposition.')
        if a.version!=d.asset_version: raise ValidationError('STALE_VERSION: propose disposition again after reviewing the changed asset.')
        if action=='approve':
            require(user,'Assigned Approver')
            if user==d.proposer or d.state!='Proposed': raise ValidationError('Independent approval of a pending proposal is required.')
            d.approver=user; d.state='Authorized'
        elif action=='complete':
            require(user,'Asset Custodian',department=a.department_id)
            if a.custodian_id!=user.pk or d.state!='Authorized': raise PermissionDenied
            d.state='Completed'; d.evidence=evidence; d.completed_at=timezone.now()
            a.status=d.action; a.published=False; a.version+=1; a.save()
        else: raise ValidationError('Invalid disposition action.')
        d.save(); audit(user,'Disposition '+action,a,d.pk,evidence=evidence,state=d.state)
