"""Periodic translation on measured, spatially repeating strips of a single source."""
import io
import math
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

from . import dds, mdx, ouroboros_media as media, raf, theme
from .config import ANIMATED_SIZES, max_frames_for

GRID = (160, 90)


def error(a, b):
    return float(np.abs(a.astype(np.float32) - b).mean())


def strips(samples):
    """In oriented coordinates translation is along x. Static rows stay protected."""
    activity = np.abs(np.diff(samples.astype(np.float32), axis=0)).mean(axis=(0, 2, 3))
    active = activity > 1.5
    edges = np.flatnonzero(np.diff(np.r_[False, active, False]))
    bands = []
    for y, end in edges.reshape(-1, 2):
        if end-y < 4:
            raise media.MotionError("A faixa móvel é estreita demais para medir.")
        seq = samples[:, y:end]
        first = seq[0]
        period = None
        for p in range(8, first.shape[1]//2+1):
            if error(first[:,p:], first[:,:-p]) < 1.8:
                period = p
                break
        if period is None:
            raise media.MotionError("A textura não apresenta duas repetições espaciais coerentes.")
        shifts = np.arange(-min(6,(period-1)//2), min(6,(period-1)//2)+1)
        observed = []
        for a,b in zip(seq,seq[1:]):
            reference=b[:,7:-7]
            costs=[error(reference,a[:,7-d:a.shape[1]-7-d]) for d in shifts]
            best=int(np.argmin(costs))
            if shifts[best]==0 or costs[best]>2.5 or costs[best]>.60*costs[len(shifts)//2]:
                raise media.MotionError("Translação insuficiente ou ambígua na faixa.")
            observed.append(int(shifts[best]))
        if len(set(observed))!=1:
            raise media.MotionError("A velocidade ou o sentido varia na amostra.")
        bands.append((int(y),int(end),period,observed[0]))
    if not bands or len(bands)>4 or len({np.sign(b[3]) for b in bands})!=1:
        raise media.MotionError("A amostra precisa de faixas coerentes no mesmo sentido.")
    return bands


def shift_frame(first,bands,t,size):
    image=np.array(Image.fromarray(first).resize(size,Image.Resampling.BILINEAR))
    regions=[]
    for a,b,p,v in bands:
        top=4*round(a*size[1]/len(first)/4)
        bottom=min(size[1],4*round(b*size[1]/len(first)/4)) if b<len(first) else size[1]
        x=(np.arange(size[0])+.5)*first.shape[1]/size[0]-.5-v*t
        left=np.floor(x).astype(int)
        w=(x-left)[None,:,None]
        tile=first[a:b,:p]
        row=np.rint(tile[:,left%p]*(1-w)+tile[:,(left+1)%p]*w).astype('uint8')
        image[top:bottom]=Image.fromarray(row).resize((size[0],bottom-top),Image.Resampling.BILINEAR)
        regions.append((top,bottom))
    return image,regions


def verify_pair(old,new,regions,steps):
    """Independently match each decoded moving strip including across the wrap."""
    protected=np.ones(len(old),bool)
    measures=[]
    for (a,b),step in zip(regions,steps):
        protected[a:b]=False
        margin=math.ceil(abs(step))+4
        if margin*2>=old.shape[1]:
            raise media.MotionError("Deslocamento excessivo para medir.")
        ref=new[a:b:2,margin:-margin:3]
        near=round(step)
        shifts=sorted({0,-near,*range(near-2,near+3)})
        costs=[error(ref,old[a:b:2,margin-d:old.shape[1]-margin-d:3]) for d in shifts]
        best=shifts[int(np.argmin(costs))]
        if best*step<=0 or abs(best-step)>1.1 or min(costs)>max(4,.55*costs[shifts.index(0)]):
            raise media.MotionError("Falha de continuidade na trajetória dos DDS.")
        measures.append(best)
    if not np.array_equal(old[protected],new[protected]):
        raise media.MotionError("A região protegida mudou após a codificação.")
    return measures


def render(path,duration,folder,size,axis,rate,progress,check):
    span=min(.8,duration)
    n=round(span*10)
    if n<4:
        raise media.MotionError("Fonte curta demais para medir a trajetória.")
    samples=media.extract(path,0,span,n,GRID,folder,check)
    vertical=axis=="vertical"
    oriented=samples.transpose(0,2,1,3) if vertical else samples
    bands=strips(oriented)
    ticks=math.lcm(*(p//math.gcd(p,abs(v)) for _,_,p,v in bands))
    count=min(4*(max_frames_for(size)//4),ticks*4)
    wait=ticks*span/(n*rate*count)
    if not .02<=wait<=.2:
        raise media.MotionError("O período comum não cabe na cadência RAF.")
    out=size[::-1] if vertical else size
    steps=[v*ticks/count*out[0]/oriented.shape[2] for _,_,_,v in bands]
    original=Image.fromarray(samples[0])
    original.save(folder/'source.png')
    total=0
    vectors=[]
    names=[]
    previous=None
    with tempfile.TemporaryDirectory(dir=folder) as tmp:
        tree=Path(tmp)
        for i in range(count):
            progress("Sintetizando e verificando DDS",int(95*i/count))
            check()
            rgb,regions=shift_frame(oriented[0],bands,ticks*i/count,out)
            frame=Image.fromarray(rgb.transpose(1,0,2) if vertical else rgb)
            blob=dds.encode_frame(np.asarray(frame.convert("RGBA")))
            name=theme.frame_name(i)
            names.append(name)
            (tree/name).write_bytes(blob)
            total+=len(blob)
            decoded=Image.open(io.BytesIO(blob)).convert("RGB")
            decoded.save(folder/f"{i:02d}.webp",lossless=True,method=0)
            pixels=np.asarray(decoded)
            current=pixels.transpose(1,0,2) if vertical else pixels
            if previous is None:
                first=current
            else:
                vectors.append(verify_pair(previous,current,regions,steps))
            previous=current
        vectors.append(verify_pair(previous,first,regions,steps))
        theme.check_frame_count(count)
        theme.check_budget(total)
        w,h=raf.plane_half_size()
        (tree/raf.MODEL_REAL_FILE).write_bytes(mdx.quad_model(node_name=raf.MODEL_ID,half_w=w,half_h=h))
        theme.write_index_xml(tree/'index.xml',raf.build_scene(names,wait))
        theme.zip_raf_folder(tree,folder/'background.raf.zip')
    return dict(type="genesis",frames=count,wait=wait,size=size,bytes=total,bands=bands,
                expected=steps,measured=vectors,axis=axis)
