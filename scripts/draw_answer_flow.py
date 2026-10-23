"""
Draw docs/answer-flow.svg, the README diagram of how one question is answered
(styled to sit next to the GitDiagram image).

    python scripts/draw_answer_flow.py docs/answer-flow.svg
"""
import sys
W,H=1200,640
FONT="Inter, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
C={ # fill, border, text, dot
 "blue":("#DBEAFE","#3B82F6","#1E3A8A","#2563EB"),
 "red":("#FFE4E6","#E11D48","#881337","#E11D48"),
 "indigo":("#E0E7FF","#6366F1","#312E81","#4F46E5"),
 "teal":("#CCFBF1","#0D9488","#134E4A","#0F766E"),
 "green":("#DCFCE7","#16A34A","#14532D","#16A34A"),
 "purple":("#F3E8FF","#9333EA","#581C87","#9333EA"),
 "orange":("#FEF3C7","#D97706","#78350F","#D97706"),
 "grey":("#F1F5F9","#64748B","#1E293B","#475569"),
}
o=[]
def esc(t): return t.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
def card(x,y,w,h):
    o.append(f'<rect x="{x+4}" y="{y+4}" width="{w}" height="{h}" rx="10" fill="#111"/>')
    o.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="#fff" stroke="#111" stroke-width="2"/>')
def head(x,y,label,col,num=None):
    o.append(f'<circle cx="{x+20}" cy="{y+25}" r="5" fill="{C[col][3]}"/>')
    t=esc(label) if num is None else f'<tspan fill="#6B7280">{num}</tspan>  {esc(label)}'
    o.append(f'<text x="{x+34}" y="{y+31}" font-family="{FONT}" font-size="17" font-weight="700" fill="#111">{t}</text>')
def pill(x,y,w,label,col,h=30,fs=15):
    f,b,t,_=C[col]
    o.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="5" fill="{f}" stroke="{b}" stroke-width="1.5"/>')
    o.append(f'<text x="{x+11}" y="{y+h/2+5}" font-family="{FONT}" font-size="{fs}" fill="{t}">{esc(label)}</text>')
def arrow(x1,y1,x2,y2,label=None,lx=None,ly=None):
    o.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#111" stroke-width="2.5" marker-end="url(#ah)"/>')
    if label:
        o.append(f'<text x="{lx}" y="{ly}" font-family="{FONT}" font-size="13" fill="#4B5563">{esc(label)}</text>')

o.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">')
o.append('<defs><marker id="ah" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#111"/></marker></defs>')
o.append(f'<rect width="{W}" height="{H}" fill="#F1E8FD"/>')
o.append(f'<text x="48" y="80" font-family="{FONT}" font-size="42" font-weight="800" fill="#111">How one question is answered</text>')
o.append(f'<text x="1152" y="78" text-anchor="end" font-family="{FONT}" font-size="22" font-weight="800" fill="#7C3AED">~1 s to first audio</text>')

stages=[("Question","blue",["Counsel speaks or types","Streaming speech-to-text","Key terms from statement","Document IDs rebuilt"]),
        ("Retrieval","red",["Dense search, top 5","Document-ID routing","Refusal gate on top score"]),
        ("Answer","indigo",["LLM picks paragraph, quote","Verbatim check in code","OCR-tolerant second pass"]),
        ("Voice","teal",["Streaming text-to-speech","Cached lead-ins, refusals","Words highlighted as read"])]
x0,y0,cw,gap,ch=48,112,246,40,212
for i,(name,col,items) in enumerate(stages):
    x=x0+i*(cw+gap)
    card(x,y0,cw,ch); head(x,y0,name,col,num=i+1)
    for j,it in enumerate(items):
        pill(x+12,y0+50+j*38,cw-24,it,col)
    if i<3:
        arrow(x+cw+8,y0+ch/2,x+cw+gap-6,y0+ch/2)

# outcomes
oy=y0+ch+62; oh=150
arrow(x0+1*(cw+gap)+cw/2,y0+ch+6,x0+1*(cw+gap)+cw/2,oy-6,"score below gate",x0+1*(cw+gap)+cw/2+10,y0+ch+36)
arrow(x0+2*(cw+gap)+cw/2,y0+ch+6,x0+2*(cw+gap)+cw/2,oy-6,"after the check",x0+2*(cw+gap)+cw/2+10,y0+ch+36)
card(x0,oy,W-96,oh); head(x0,oy,"What the witness says","green")
outs=[("Verified quote","green","found word for word in a retrieved paragraph"),
      ("OCR-corrected quote","purple","matched once scanning errors are allowed for"),
      ("Referral to the paragraph","orange","quote didn't verify, so nothing is quoted"),
      ("Refusal","grey","nothing relevant retrieved, or no answer")]
ow=(W-96-24-3*16)/4
for k,(lab,col,cap) in enumerate(outs):
    x=x0+12+k*(ow+16)
    pill(x,oy+52,ow,lab,col,h=34,fs=16)
    words=cap.split(); lines=[];cur=""
    for wd in words:
        if len(cur+" "+wd)>30: lines.append(cur); cur=wd
        else: cur=(cur+" "+wd).strip()
    lines.append(cur)
    for li,l in enumerate(lines):
        o.append(f'<text x="{x+2}" y="{oy+108+li*19}" font-family="{FONT}" font-size="14" fill="#4B5563">{esc(l)}</text>')

o.append(f'<text x="48" y="{H-30}" font-family="{FONT}" font-size="18" fill="#4B5563">Every answer is quoted from the statement, referred or refused · nothing is generated</text>')
o.append('</svg>')
open(sys.argv[1],"w").write("\n".join(o))
