def navigation(request):
    if not request.user.is_authenticated: return {}
    p=getattr(request.user,'profile',None)
    return {'campus_profile':p,'unread':request.user.notice_set.filter(read=False).count()}
