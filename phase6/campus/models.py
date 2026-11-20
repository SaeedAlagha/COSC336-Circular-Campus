import uuid
from django.conf import settings
from django.db import models
from django.db.models import Q, F
from django.core.validators import MinValueValidator
from django.utils import timezone

class Department(models.Model):
    name=models.CharField(max_length=100,unique=True)
    active=models.BooleanField(default=True)
    def __str__(self): return self.name

ROLES=[(x,x) for x in ['Requester','Department Representative','Asset Custodian','Assigned Approver','Maintenance Staff','Procurement Officer','Finance Officer','Sustainability Officer','System Administrator','Management / Auditor']]
class Profile(models.Model):
    user=models.OneToOneField(settings.AUTH_USER_MODEL,on_delete=models.CASCADE)
    department=models.ForeignKey(Department,on_delete=models.PROTECT)
    role=models.CharField(max_length=35,choices=ROLES)
    def __str__(self): return f'{self.user} · {self.role}'

CATEGORIES=[(x,x) for x in ['Computers','Electronics','Furniture','Laboratory','Books','Office']]
CONDITIONS=[('Excellent','Excellent'),('Good','Good'),('Fair','Fair'),('Poor','Poor')]
class Asset(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    code=models.CharField(max_length=24,unique=True)
    name=models.CharField(max_length=160)
    description=models.TextField()
    category=models.CharField(max_length=30,choices=CATEGORIES)
    tags=models.CharField(max_length=250,blank=True)
    quantity=models.PositiveIntegerField(validators=[MinValueValidator(1)])
    department=models.ForeignKey(Department,on_delete=models.PROTECT)
    custodian=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='assets')
    campus=models.CharField(max_length=60,default='Main Campus')
    building=models.CharField(max_length=60)
    room=models.CharField(max_length=30)
    purchase_date=models.DateField(null=True,blank=True)
    value=models.DecimalField(max_digits=12,decimal_places=2,null=True,blank=True,validators=[MinValueValidator(0)])
    condition=models.CharField(max_length=15,choices=CONDITIONS,default='Good')
    status=models.CharField(max_length=25,choices=[(x,x) for x in ['Available','Restricted','Retired','Donated','Recycled','Disposed']],default='Available')
    published=models.BooleanField(default=False)
    ready_date=models.DateField(default=timezone.localdate)
    unwanted=models.BooleanField(default=True)
    version=models.PositiveIntegerField(default=1)
    unit_start=models.PositiveIntegerField(default=0)
    parent=models.ForeignKey('self',null=True,blank=True,on_delete=models.PROTECT)
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering=['code']
        constraints=[models.CheckConstraint(condition=Q(quantity__gte=1),name='asset_positive_stock')]
    def __str__(self): return f'{self.code} · {self.name}'
    @property
    def free_quantity(self):
        held=sum(h.quantity for h in self.holds.filter(state__in=['Pending Approval','Allocated','Released','Exception']).filter(Q(state__in=['Allocated','Released','Exception'])|Q(expires_at__gt=timezone.now())))
        return max(0,self.quantity-held) if self.status=='Available' and self.published else 0

class Attachment(models.Model):
    asset=models.ForeignKey(Asset,on_delete=models.PROTECT,related_name='attachments')
    file=models.FileField(upload_to='assets/%Y/%m/')
    name=models.CharField(max_length=200)
    uploaded_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)

class Need(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    title=models.CharField(max_length=160)
    purpose=models.TextField()
    specifications=models.TextField(help_text='Required technical terms, separated by commas')
    category=models.CharField(max_length=30,choices=CATEGORIES)
    quantity=models.PositiveIntegerField(validators=[MinValueValidator(1)])
    minimum_condition=models.CharField(max_length=15,choices=CONDITIONS,default='Good')
    urgency=models.CharField(max_length=10,choices=[(x,x) for x in ['High','Normal','Low']],default='Normal')
    required_date=models.DateField()
    building=models.CharField(max_length=60)
    room=models.CharField(max_length=30)
    campus=models.CharField(max_length=60,default='Main Campus')
    allow_partial=models.BooleanField(default=False)
    requester=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    department=models.ForeignKey(Department,on_delete=models.PROTECT)
    state=models.CharField(max_length=20,default='Open')
    received=models.PositiveIntegerField(default=0)
    version=models.PositiveIntegerField(default=1)
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering=['-created_at']
        constraints=[models.CheckConstraint(condition=Q(quantity__gte=1)&Q(received__lte=F('quantity')),name='need_valid_quantities')]
    def __str__(self): return self.title

class Hold(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    need=models.ForeignKey(Need,on_delete=models.PROTECT,related_name='holds')
    asset=models.ForeignKey(Asset,on_delete=models.PROTECT,related_name='holds')
    quantity=models.PositiveIntegerField(validators=[MinValueValidator(1)])
    state=models.CharField(max_length=25,default='Pending Approval')
    proposer=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='proposals')
    approver=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='assigned_proposals')
    receiver=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='incoming_transfers')
    source_custodian=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='outgoing_transfers')
    version=models.PositiveIntegerField(default=1)
    asset_version=models.PositiveIntegerField()
    safety_review=models.BooleanField(default=False)
    cost=models.DecimalField(max_digits=12,decimal_places=2,default=0,validators=[MinValueValidator(0)])
    cost_review=models.BooleanField(default=False)
    expires_at=models.DateTimeField()
    created_at=models.DateTimeField(auto_now_add=True)
    release_evidence=models.TextField(blank=True)
    receipt_evidence=models.TextField(blank=True)
    reason=models.TextField(blank=True)
    result_asset=models.ForeignKey(Asset,on_delete=models.PROTECT,null=True,blank=True,related_name='received_transfers')
    completed_at=models.DateTimeField(null=True,blank=True)
    completed_root=models.UUIDField(null=True,blank=True)
    completed_start=models.PositiveIntegerField(null=True,blank=True)
    idempotency_key=models.UUIDField(unique=True)
    class Meta:
        constraints=[models.CheckConstraint(condition=Q(quantity__gte=1)&Q(cost__gte=0),name='hold_valid_quantities_cost')]

class Decision(models.Model):
    hold=models.ForeignKey(Hold,on_delete=models.PROTECT,related_name='decisions')
    actor=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    version=models.PositiveIntegerField()
    decision=models.CharField(max_length=40)
    reason=models.TextField()
    created_at=models.DateTimeField(auto_now_add=True)

class Service(models.Model):
    asset=models.ForeignKey(Asset,on_delete=models.PROTECT,related_name='services')
    reporter=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='reported_services')
    defect=models.TextField()
    findings=models.TextField(blank=True)
    work=models.TextField(blank=True)
    cost=models.DecimalField(max_digits=12,decimal_places=2,null=True,blank=True,validators=[MinValueValidator(0)])
    condition_before=models.CharField(max_length=15)
    condition_after=models.CharField(max_length=15,choices=CONDITIONS,blank=True)
    state=models.CharField(max_length=30,default='Inspection required')
    staff=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True,blank=True,related_name='service_work')
    verifier=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True,blank=True,related_name='verified_service')
    evidence=models.TextField(blank=True)
    created_at=models.DateTimeField(auto_now_add=True)
    completed_at=models.DateTimeField(null=True,blank=True)

class Disposition(models.Model):
    asset=models.ForeignKey(Asset,on_delete=models.PROTECT,related_name='dispositions')
    action=models.CharField(max_length=20,choices=[(x,x) for x in ['Donated','Recycled','Disposed']])
    proposer=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='dispositions')
    approver=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True,blank=True,related_name='disposition_approvals')
    reason=models.TextField()
    alternatives=models.TextField()
    evidence=models.TextField(blank=True)
    state=models.CharField(max_length=20,default='Proposed')
    asset_version=models.PositiveIntegerField()
    created_at=models.DateTimeField(auto_now_add=True)
    completed_at=models.DateTimeField(null=True,blank=True)

class Impact(models.Model):
    hold=models.OneToOneField(Hold,on_delete=models.PROTECT)
    reviewer=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    replacement_value=models.DecimalField(max_digits=12,decimal_places=2,validators=[MinValueValidator(0)])
    transfer_cost=models.DecimalField(max_digits=12,decimal_places=2,validators=[MinValueValidator(0)])
    repair_cost=models.DecimalField(max_digits=12,decimal_places=2,validators=[MinValueValidator(0)])
    refurbishment_cost=models.DecimalField(max_digits=12,decimal_places=2,validators=[MinValueValidator(0)])
    other_cost=models.DecimalField(max_digits=12,decimal_places=2,validators=[MinValueValidator(0)])
    replacement_carbon=models.DecimalField(max_digits=12,decimal_places=3,null=True,blank=True,validators=[MinValueValidator(0)])
    reuse_carbon=models.DecimalField(max_digits=12,decimal_places=3,null=True,blank=True,validators=[MinValueValidator(0)])
    baseline_retirement=models.DateField(null=True,blank=True)
    revised_retirement=models.DateField(null=True,blank=True)
    evidence=models.TextField()
    factor_source=models.CharField(max_length=250,blank=True)
    created_at=models.DateTimeField(auto_now_add=True)
    @property
    def savings(self): return self.replacement_value-self.transfer_cost-self.repair_cost-self.refurbishment_cost-self.other_cost
    @property
    def carbon(self):
        return self.replacement_carbon-self.reuse_carbon if self.replacement_carbon is not None and self.reuse_carbon is not None and self.factor_source else None

class Event(models.Model):
    asset=models.ForeignKey(Asset,on_delete=models.PROTECT,null=True,blank=True,related_name='events')
    actor=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    action=models.CharField(max_length=80)
    reference=models.CharField(max_length=100,blank=True)
    detail=models.JSONField(default=dict)
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta: ordering=['-created_at']

class Notice(models.Model):
    user=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.CASCADE)
    message=models.CharField(max_length=250)
    read=models.BooleanField(default=False)
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta: ordering=['-created_at']

class AIResult(models.Model):
    actor=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    function=models.CharField(max_length=20)
    provider=models.CharField(max_length=100)
    input_hash=models.CharField(max_length=64)
    output=models.JSONField(default=dict)
    status=models.CharField(max_length=20)
    review=models.CharField(max_length=20,blank=True)
    review_reason=models.TextField(blank=True)
    created_at=models.DateTimeField(auto_now_add=True)

class ReportSnapshot(models.Model):
    actor=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    kind=models.CharField(max_length=50)
    scope=models.CharField(max_length=100)
    start=models.DateField()
    end=models.DateField()
    data=models.JSONField()
    narrative=models.TextField(blank=True)
    reviewed=models.BooleanField(default=False)
    created_at=models.DateTimeField(auto_now_add=True)
