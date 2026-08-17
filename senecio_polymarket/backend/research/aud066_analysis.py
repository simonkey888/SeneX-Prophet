"""AUD-066 extended proper-score and walk-forward evaluation."""
from __future__ import annotations
import math, random
from .aud066_liquidation import BASE_FEATURES,REALIZED_FEATURES,NORMALIZED_FEATURES,fit_logit,predict

def _sig(z):
    z=max(-35,min(35,z)); return 1/(1+math.exp(-z))

def calibration_intercept_slope(y,p):
    """Logistic calibration y ~ intercept + slope*logit(p), Newton solved."""
    if len(y)<20 or len(set(y))<2: return None,None
    x=[math.log(max(1e-9,min(1-1e-9,q))/(1-max(1e-9,min(1-1e-9,q)))) for q in p]
    a,b=0.0,1.0
    for _ in range(30):
        g0=g1=h00=h01=h11=0.0
        for yi,xi in zip(y,x):
            q=_sig(a+b*xi); w=max(q*(1-q),1e-9); e=yi-q
            g0+=e; g1+=e*xi; h00+=w; h01+=w*xi; h11+=w*xi*xi
        det=h00*h11-h01*h01
        if abs(det)<1e-12: break
        da=(g0*h11-g1*h01)/det; db=(g1*h00-g0*h01)/det
        a+=da; b+=db
        if max(abs(da),abs(db))<1e-7: break
    return a,b

def metrics(y,p):
    n=len(y)
    if not n:return {'n':0}
    p=[max(1e-9,min(1-1e-9,float(q))) for q in p]
    brier=sum((q-t)**2 for q,t in zip(p,y))/n
    ll=-sum(t*math.log(q)+(1-t)*math.log(1-q) for t,q in zip(y,p))/n
    pred=[q>=.5 for q in p]; acc=sum(int(a)==b for a,b in zip(pred,y))/n
    pos=sum(y); neg=n-pos; tpr=sum(bool(a) and b==1 for a,b in zip(pred,y))/pos if pos else 0; tnr=sum((not a) and b==0 for a,b in zip(pred,y))/neg if neg else 0
    ece=0.0
    for k in range(10):
        ids=[i for i,q in enumerate(p) if k/10<=q<((k+1)/10 if k<9 else 1.0000001)]
        if ids: ece+=len(ids)/n*abs(sum(p[i] for i in ids)/len(ids)-sum(y[i] for i in ids)/len(ids))
    ci,cs=calibration_intercept_slope(y,p)
    return {'n':n,'brier':brier,'log_loss':ll,'ece_10':ece,'calibration_intercept':ci,'calibration_slope':cs,'accuracy':acc,'balanced_accuracy':(tpr+tnr)/2}

def _block_ci(values,seed=66,reps=5000):
    if len(values)<2:return [None,None]
    rng=random.Random(seed); n=len(values); means=[]
    for _ in range(reps): means.append(sum(values[rng.randrange(n)] for __ in range(n))/n)
    means.sort(); return [means[int(.025*(reps-1))],means[int(.975*(reps-1))]]

def walk_forward_extended(samples):
    days=sorted({r['day'] for r in samples})
    if len(days)<7:return {'status':'INCONCLUSIVE','reason':'INSUFFICIENT_INDEPENDENT_DAYS','days':days}
    arms={'A':BASE_FEATURES,'B':BASE_FEATURES+REALIZED_FEATURES,'C':BASE_FEATURES+REALIZED_FEATURES+NORMALIZED_FEATURES}
    blocks=[]
    for td in days[-3:]:
        i=days.index(td); vd=days[i-1]; trdays=days[:i-1]
        tr=[r for r in samples if r['day'] in trdays]; va=[r for r in samples if r['day']==vd]; te=[r for r in samples if r['day']==td]
        if min(len(tr),len(va),len(te))<30: continue
        models={a:fit_logit(tr,n) for a,n in arms.items()}; yv=[r['y'] for r in va]; yt=[r['y'] for r in te]
        vm={a:metrics(yv,predict(m,va)) for a,m in models.items()}; tm={a:metrics(yt,predict(m,te)) for a,m in models.items()}
        chosen=min(arms,key=lambda a:(vm[a]['brier'],len(arms[a])))
        blocks.append({'train_days':trdays,'validation_day':vd,'test_day':td,'train_n':len(tr),'validation_n':len(va),'test_n':len(te),'validation':vm,'test':tm,'chosen_E':chosen})
    if len(blocks)<2:return {'status':'INCONCLUSIVE','reason':'INSUFFICIENT_VALID_WALK_FORWARD_BLOCKS','days':days,'blocks':blocks}
    keys=('brier','log_loss','ece_10','calibration_intercept','calibration_slope','accuracy','balanced_accuracy')
    summary={}
    for a in arms:
        summary[a]={}
        for k in keys:
            vals=[b['test'][a][k] for b in blocks if b['test'][a].get(k) is not None]
            summary[a][k]=sum(vals)/len(vals) if vals else None
    deltas={}
    for a in ('B','C'):
        db=[b['test'][a]['brier']-b['test']['A']['brier'] for b in blocks]
        dl=[b['test'][a]['log_loss']-b['test']['A']['log_loss'] for b in blocks]
        deltas[a]={'brier_by_block':db,'log_loss_by_block':dl,'mean_brier_delta':sum(db)/len(db),'mean_log_loss_delta':sum(dl)/len(dl),'brier_delta_block_bootstrap_95':_block_ci(db,66),'log_loss_delta_block_bootstrap_95':_block_ci(dl,660)}
    better={a:sum(b['test'][a]['brier']<b['test']['A']['brier'] and b['test'][a]['log_loss']<b['test']['A']['log_loss'] for b in blocks) for a in ('B','C')}
    best=min(('B','C'),key=lambda a:(summary[a]['brier'],summary[a]['log_loss']))
    yes=better[best]>=2 and deltas[best]['mean_brier_delta']<0 and deltas[best]['mean_log_loss_delta']<0
    # If CI crosses zero, evidence is fragile even if mean improves; classify INCONCLUSIVE rather than YES.
    ci_b=deltas[best]['brier_delta_block_bootstrap_95']; ci_l=deltas[best]['log_loss_delta_block_bootstrap_95']
    robust=yes and ci_b[1] is not None and ci_l[1] is not None and ci_b[1]<0 and ci_l[1]<0
    realized='YES' if robust else ('INCONCLUSIVE' if yes else 'NO')
    return {'status':'COMPLETE','days':days,'blocks':blocks,'summary':summary,'deltas_vs_A':deltas,'better_blocks':better,'best_realized_arm':best,'mean_improvement_pass':yes,'ci_excludes_zero_pass':robust,'realized_value':realized}
