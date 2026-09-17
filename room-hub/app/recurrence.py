"""Finite recurrence, inclusive end; Monday=0. Monthly clamping preserves original day."""
from datetime import timedelta
from calendar import monthrange
from .models import TaskCreate

def occurrences(task:TaskCreate):
    start,r=task.date,task.repeat
    if r.frequency=='none': return [start]
    anchor=start-timedelta(days=start.weekday());d=start;out=[]
    while d<=r.until:
        delta=(d-start).days;week=(d-anchor).days//7;ok=False
        if r.frequency=='daily': ok=delta%r.interval==0
        elif r.frequency=='weekdays':ok=d.weekday()<5 and week%r.interval==0
        elif r.frequency=='weekly':ok=d.weekday() in (r.weekdays or [start.weekday()]) and week%r.interval==0
        elif r.frequency=='monthly':
            month=(d.year-start.year)*12+d.month-start.month
            ok=month%r.interval==0 and d.day==min(start.day,monthrange(d.year,d.month)[1])
        if ok:out.append(d)
        d+=timedelta(days=1)
    if not out:raise ValueError('지정 조건에 해당하는 날짜가 없습니다.')
    return out
