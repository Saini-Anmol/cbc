import os as _o, sys as _s; _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))  # allow imports from repo root
"""Regenerate the tail of plant_may_production_insights.pdf:
   page 5 = OUR JULY PLAN (margin=150/budget=2, 690,835 / 127 diff-COs)
   page 6 = OUR JULY PLAN (margin=200/budget=2, 683,536 / 83 diff-COs)
   page 7 = Appendix B recommendations (carried, renumbered)
Keeps existing pages 1-4 (plant analysis) via a fitz merge."""
import os, json, sys
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import FancyBboxPatch
import fitz
sys.path.insert(0, "/Users/anmolsaini/Documents/cbc")
from bc_config import BUILDING_MACHINE_NAMES

INK="#1a2332"; BLUE="#2563eb"; GREEN="#15803d"; RED="#dc2626"; AMBER="#b45309"; GREY="#64748b"
LBLUE="#dbeafe"; LGREEN="#dcfce7"
SP="/private/tmp/claude-501/-Users-anmolsaini-Documents-cbc/73d76e07-b526-4396-8fdc-50682e5a7c27/scratchpad"
PDF="/Users/anmolsaini/Documents/cbc/plant_may_production_insights.pdf"

STAGE1={'6801','6802','6803','6909','6911','7601','7701','7801','7802','7803','7804','8001','8002','8003','8101'}
STAGE2={'8201','8301','8302','8501','8502','7301'}
def mgrp(c): return 'Stage-1' if c in STAGE1 else 'Stage-2' if c in STAGE2 else 'Unistage'
def _num(i):
    try: return int(i)
    except Exception: return 99

def newpage(pdf):
    fig=plt.figure(figsize=(8.5,11)); fig.patch.set_facecolor('white'); return fig
def header(fig,kicker,title,tsize=17):
    fig.text(0.06,0.955,kicker,fontsize=10,color=BLUE,weight='bold',ha='left')
    fig.text(0.06,0.925,title,fontsize=tsize,color=INK,weight='bold',ha='left',va='top')
    fig.lines.append(plt.Line2D([0.06,0.94],[0.905,0.905],color='#cbd5e1',lw=1,transform=fig.transFigure))
def footer(fig,n):
    fig.text(0.94,0.03,f"JK Tyre B2C — plan analysis  •  page {n}",fontsize=7.5,color=GREY,ha='right')

def machine_page(pdf,kicker,title,subtitle,data,flexset,keyread,keycolor,keyface,pagenum):
    fig=newpage(pdf); header(fig,kicker,title,tsize=16)
    fig.text(0.06,0.875,subtitle,fontsize=9,color=GREY,va='top')
    def drawcol(x, groups):
        y=0.835
        for gname in groups:
            fig.text(x,y,gname,fontsize=9.5,color=BLUE,weight='bold'); y-=0.026
            fig.text(x,y,"code   plant name          sizes",fontsize=7.5,color=GREY); y-=0.020
            for code in sorted([c for c in data if data[c][0]==gname], key=_num):
                gr,pn,sz=data[code]; bold=code in flexset
                col=AMBER if bold else INK; w='bold' if bold else 'normal'
                fig.text(x,       y,code,fontsize=8.3,color=col,weight=w,family='monospace')
                fig.text(x+0.055, y,str(pn)[:16],fontsize=8.3,color=INK,family='monospace')
                fig.text(x+0.20,  y,(", ".join(sz) if sz else "(idle)")+'"',fontsize=8.3,color=col,weight=w)
                y-=0.0205
            y-=0.012
    drawcol(0.06, ['Stage-1','Stage-2']); drawcol(0.52, ['Unistage'])
    ax=fig.add_axes([0.06,0.06,0.88,0.082]); ax.axis('off')
    ax.add_patch(FancyBboxPatch((0,0),1,1,boxstyle="round,pad=0.02,rounding_size=0.05",fc=keyface,ec=keycolor,lw=1.2,transform=ax.transAxes))
    ax.text(0.5,0.74,"KEY READ",fontsize=8.5,color=keycolor,weight='bold',ha='center')
    ax.text(0.5,0.34,keyread,fontsize=8.7,color=INK,ha='center',va='center')
    footer(fig,pagenum); pdf.savefig(fig,facecolor='white'); plt.close(fig)

def build_data(jsonfile):
    raw=json.load(open(jsonfile))
    d={}
    for code,pname in BUILDING_MACHINE_NAMES.items():
        code=str(code); d[code]=(mgrp(code),pname,raw.get(code,[]))
    flex=[c for c in d if len(d[c][2])>=2]
    return d, set(flex)

# ---- generate the 3 tail pages ----
tmp=SP+"/_july_tail.pdf"
pdf=PdfPages(tmp)

d150,f150=build_data(SP+"/our_july_150.json")
machine_page(pdf,"APPENDIX A2 — OUR JULY PLAN  (margin=150, budget=2)",
    "690,835 cured  ·  88.6% coverage  ·  127 diff-size COs",
    f"What size each machine runs under the adopted July config ({len(f150)} machines multi-inch, amber). JIT + hybrid re-anchoring.",
    d150,f150,
    "Adopted July config (CO limit 10). 690,835 cured / 127 diff-size COs — the balanced knee: near-peak KPI\n"
    "with plant-like churn. Machines concentrate on a dominant inch; only the amber ones flex across 2-3.",
    GREEN,LGREEN,5)

d200,f200=build_data(SP+"/our_july_200.json")
machine_page(pdf,"APPENDIX A3 — OUR JULY PLAN  (margin=200, budget=2)",
    "683,536 cured  ·  87.7% coverage  ·  83 diff-size COs",
    f"Leaner-churn alternative ({len(f200)} machines multi-inch, amber). Higher urgency margin = fewer size switches.",
    d200,f200,
    "Lower-churn July alternative: only 83 diff-size COs (vs 127) but ~7.3k less cured. Raising the urgency margin\n"
    "100→200 trims switching but costs coverage — use only if minimising changeovers matters more than KPI.",
    AMBER,"#fef3c7",6)

# ---- page 7: recommendations ----
def action(fig, y, num, title, color):
    fig.text(0.06,y,num+".",fontsize=12.5,color=color,weight='bold')
    fig.text(0.095,y,title,fontsize=12,color=INK,weight='bold',va='baseline')
def step(fig, y, label, color, bodylines):
    fig.text(0.075,y,"•",fontsize=10,color=color,weight='bold',va='top')
    fig.text(0.098,y,label,fontsize=9.4,color=color,weight='bold',va='top')
    for i,ln in enumerate(bodylines):
        fig.text(0.098,y-0.021-0.019*i,ln,fontsize=8.9,color=INK,va='top')

fig=newpage(pdf); header(fig,"APPENDIX B — WHAT WE DID","The adopted approach (validated on May/June/July)",tsize=17)
action(fig,0.875,"1","Replaced the 5-day rule with JIT switching + a daily CO budget",GREEN)
step(fig,0.840,"Trigger (JIT)",GREEN,
     ["A machine changes size the moment a curing press needs a size it has no GT for —",
      "no dwell, no cooldown. This unlocks the switches the plant makes (median ~7 h)."])
step(fig,0.777,"Control (no bounce to 293)",GREEN,
     ["An urgency margin (only switch if the target inch is meaningfully more starving) + a per-machine",
      "per-day diff-CO budget. Holds churn to ~100-150 diff-COs while keeping the KPI."])
step(fig,0.714,"Kept",GREEN,
     ["Demand-complete immediate switch, and Stage-1 single-inch (validated — the plant keeps it too)."])
ax=fig.add_axes([0.06,0.60,0.88,0.058]); ax.axis('off')
ax.add_patch(FancyBboxPatch((0,0),1,1,boxstyle="round,pad=0.02,rounding_size=0.05",fc=LGREEN,ec=GREEN,lw=1.2,transform=ax.transAxes))
ax.text(0.5,0.66,"Result vs the 5-day dwell rule (each month, own demand + moulds + building data):",fontsize=8.8,color=INK,ha='center',weight='bold')
ax.text(0.5,0.28,"May 671,138→685,002   ·   June 610,159→644,803   ·   July 640,578→690,835   — all mould-feasible.",fontsize=8.8,color=GREEN,ha='center',weight='bold')

action(fig,0.535,"2","Seeded each month from its real building-running data + demand re-anchoring",BLUE)
step(fig,0.500,"Hybrid initial allocation",BLUE,
     ["Each machine keeps its real running size when that size has demand this month; otherwise it is",
      "re-anchored to the neediest inch. Fully demand-dynamic (5-6 machines re-anchored per month)."])
step(fig,0.437,"Month-specific inputs",BLUE,
     ["Demand file, running-moulds table, and building-running snapshot per month; curing CO limit 12",
      "for May/June, 10 for July (from the CO-limit sweep)."])
ax=fig.add_axes([0.06,0.30,0.88,0.075]); ax.axis('off')
ax.add_patch(FancyBboxPatch((0,0),1,1,boxstyle="round,pad=0.02,rounding_size=0.05",fc="#fef3c7",ec=AMBER,lw=1.2,transform=ax.transAxes))
ax.text(0.5,0.70,"BOTTOM LINE",fontsize=8.5,color=AMBER,weight='bold',ha='center')
ax.text(0.5,0.34,"Dropping the dwell and switching size JIT — controlled by urgency margin + daily budget — reaches plant-like\n"
        "output on every month, with churn tunable via the margin (higher = fewer diff-size COs, small KPI cost).",
        fontsize=8.7,color=INK,ha='center',va='center')
footer(fig,7); pdf.savefig(fig,facecolor='white'); plt.close(fig)
pdf.close()

# ---- merge: existing pages 1-4 + new 3 pages ----
old=fitz.open(PDF); new=fitz.open(tmp); out=fitz.open()
out.insert_pdf(old, from_page=0, to_page=3)   # keep pages 1-4
out.insert_pdf(new, from_page=0, to_page=2)   # July m150, July m200, recommendations
outtmp=PDF+".tmp"; out.save(outtmp); out.close(); old.close(); new.close()
os.replace(outtmp, PDF)
print("WROTE", PDF, "pages:", len(fitz.open(PDF)))
