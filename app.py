
import streamlit as st, math, os, tempfile, sqlite3, re, io
from pathlib import Path

DB=Path(__file__).with_name("quotes.db")
def db():
    c=sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS quotes(
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    company TEXT,name TEXT,email TEXT,material TEXT,height REAL,pieces INTEGER,
    path REAL,cutting REAL,price REAL,note TEXT,status TEXT DEFAULT 'NEW')""")
    c.execute("""CREATE TABLE IF NOT EXISTS calibrations(
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    label TEXT, material TEXT, height REAL, path REAL, passes INTEGER, pieces INTEGER DEFAULT 1,
    starts INTEGER DEFAULT 1, centering REAL DEFAULT 0, other_aux REAL DEFAULT 0,
    actual_total REAL, machine TEXT, source TEXT, verified INTEGER DEFAULT 1, note TEXT)""")
    # Seed only as reference records. Point #1 is kept unverified because the 40 min vs. centering
    # split still needs confirmation; point #2 has no exact path yet.
    if c.execute("SELECT COUNT(*) FROM calibrations").fetchone()[0] == 0:
        c.execute("""INSERT INTO calibrations(label,material,height,path,passes,pieces,starts,centering,actual_total,source,verified,note)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                  ("Kalibrace #1 – ozubení 1071.000","C55",29.0,107.4,2,1,1,3.0,40.0,"reálná zakázka",0,
                   "Geometrie 107.4 mm ověřena z DXF. Potvrdit, zda 40 min zahrnuje 3 min středění."))
        c.execute("""INSERT INTO calibrations(label,material,height,path,passes,pieces,starts,centering,actual_total,source,verified,note)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                  ("Kalibrace #2 – 2 drážky 12 P9","Běžná ocel",30.0,None,2,1,2,0.0,30.0,"reálná zakázka",1,
                   "30 min = celý čas operace včetně středění/najetí/navlékání. Přesná dráha zatím chybí."))
        c.commit()
    return c

def pts_for(e,tol=.03):
    t=e.dxftype()
    if t=="LINE": return [(e.dxf.start.x,e.dxf.start.y),(e.dxf.end.x,e.dxf.end.y)]
    if t in ("ARC","SPLINE"): return [(p.x,p.y) for p in e.flattening(tol)]
    if t=="CIRCLE":
        cx,cy,r=e.dxf.center.x,e.dxf.center.y,e.dxf.radius
        return [(cx+r*math.cos(i*2*math.pi/120),cy+r*math.sin(i*2*math.pi/120)) for i in range(121)]
    if t=="LWPOLYLINE":
        pts=[(p[0],p[1]) for p in e.get_points()]
        if e.closed and pts and pts[0]!=pts[-1]: pts.append(pts[0])
        return pts
    return []

def analyze(upload):
    import ezdxf
    import networkx as nx

    SNAP=0.03
    def snap(p):
        return (round(float(p[0])/SNAP)*SNAP, round(float(p[1])/SNAP)*SNAP)
    def plen(pts):
        return sum(math.hypot(pts[i+1][0]-pts[i][0],pts[i+1][1]-pts[i][1]) for i in range(len(pts)-1))

    with tempfile.NamedTemporaryFile(delete=False,suffix=".dxf") as f:
        f.write(upload.getvalue()); fn=f.name
    try:
        doc=ezdxf.readfile(fn)
        edges=[]; raw=[]
        for ent in doc.modelspace():
            try:
                pts=pts_for(ent)
                if len(pts)<2: continue
                clean=[]
                for p in pts:
                    q=snap(p)
                    if not clean or q!=clean[-1]: clean.append(q)
                if len(clean)<2: continue
                L=plen(clean)
                if L<0.02: continue
                edges.append({"a":clean[0],"b":clean[-1],"pts":clean,"length":L})
                raw.append(clean)
            except: pass

        G=nx.Graph()
        for i,e in enumerate(edges):
            G.add_edge(e["a"],e["b"],idx=i,weight=e["length"])

        candidates=[]

        # Key improvement: EDM feature may be an OPEN machining path, not a closed
        # polygon. Detect "mouth" nodes where long straight geometry leaves a
        # complex profile, then isolate the alternate path around that feature.
        mouth_nodes=[]
        for n in G.nodes:
            long_out=[]
            for nb in G.neighbors(n):
                d=G[n][nb]
                if d["weight"]>=5.0:
                    long_out.append((nb,d))
            if long_out:
                mouth_nodes.append(n)

        pairs=[]
        for i,u in enumerate(mouth_nodes):
            for v in mouth_nodes[i+1:]:
                # Typical slot/gear mouth: same X, small vertical opening.
                if abs(u[0]-v[0])<=0.12 and 0.2<abs(u[1]-v[1])<=8.0:
                    pairs.append((u,v))

        for u,v in pairs:
            H=G.copy()
            # Remove long edges leaving the two mouth nodes. This prevents the
            # path from closing through the rest of the workpiece.
            for n in (u,v):
                for nb in list(H.neighbors(n)):
                    if H[n][nb]["weight"]>=5.0:
                        H.remove_edge(n,nb)
            if not nx.has_path(H,u,v):
                continue
            try:
                nodes=nx.shortest_path(H,u,v,weight="weight")
            except:
                continue
            if len(nodes)<8: continue

            coords=[]; length=0.0
            for j in range(len(nodes)-1):
                x,y=nodes[j],nodes[j+1]
                d=H[x][y]; e=edges[d["idx"]]
                seg=e["pts"] if e["a"]==x else list(reversed(e["pts"]))
                if coords: coords.extend(seg[1:])
                else: coords.extend(seg)
                length+=e["length"]

            xs=[p[0] for p in coords]; ys=[p[1] for p in coords]
            w=max(xs)-min(xs); h=max(ys)-min(ys)
            if length<20 or w<5 or h<5: continue
            candidates.append({
                "length":float(length),"area":0.0,
                "bounds":(float(min(xs)),float(min(ys)),float(max(xs)),float(max(ys))),
                "coords":coords,"edge_count":len(nodes)-1,
                "kind":"open_profile",
                "mouth":(u,v)
            })

        # Deduplicate feature candidates.
        uniq=[]
        seen=set()
        for c in candidates:
            sig=(round(c["length"],1),round(c["bounds"][0],1),round(c["bounds"][1],1),
                 round(c["bounds"][2],1),round(c["bounds"][3],1))
            if sig not in seen:
                seen.add(sig); uniq.append(c)

        # Most detailed/compact profiles first.
        uniq.sort(key=lambda c:(-c["edge_count"], c["length"]))
        return sum(e["length"] for e in edges),uniq,raw
    finally:
        try: os.unlink(fn)
        except: pass

def plot(raw,cs,selected):
    import plotly.graph_objects as go
    fig=go.Figure()
    for co in raw:
        fig.add_trace(go.Scatter(x=[p[0] for p in co],y=[p[1] for p in co],
            mode="lines",line=dict(width=1),hoverinfo="skip",showlegend=False))
    for i,c in enumerate(cs):
        co=c["coords"]
        fig.add_trace(go.Scatter(x=[p[0] for p in co],y=[p[1] for p in co],
            mode="lines",line=dict(width=7 if i==selected else 3),
            customdata=[i]*len(co),name=f"Kontura {i+1}",
            hovertemplate=f"Kontura {i+1}<br>{c['length']:.1f} mm<extra></extra>",showlegend=False))
    fig.update_layout(height=500,margin=dict(l=5,r=5,t=40,b=5),
        title="Klikni / vyber konturu, která se skutečně řeže",
        xaxis_title="mm",yaxis_title="mm",yaxis=dict(scaleanchor="x",scaleratio=1))
    return fig

def extract_pdf_hints(upload):
    """Conservative PDF helper. Reads title-block material and stock dimensions, never invents EDM path."""
    hints={"material":None,"material_raw":None,"height":None,"stock_dims":None,"text":"","warnings":[]}
    try:
        import fitz
        doc=fitz.open(stream=upload.getvalue(), filetype="pdf")
        text="\n".join(page.get_text("text") for page in doc)
        hints["text"]=text[:12000]

        # Common material designations. Preserve exact grade when present.
        material_patterns=[
            (r"\b1[\.,]4034\b", "1.4034"),
            (r"\b1[\.,]4305\b", "1.4305"),
            (r"\b1[\.,]4301\b", "1.4301"),
            (r"\b1[\.,]2379\b", "1.2379"),
            (r"\b1[\.,]2343\b", "1.2343"),
            (r"\bC55\b", "C55"),
            (r"\bS235\b", "S235 / JS235"),
            (r"\bMild\s+steel\b", "Běžná ocel"),
            (r"\bTool\s+Steel\b", "Nástrojová ocel"),
            (r"\bWerkzeugstahl\b", "Nástrojová ocel"),
        ]
        for pat,label in material_patterns:
            if re.search(pat,text,re.I):
                hints["material"]=label; hints["material_raw"]=label; break

        # Explicit thickness/height wording has highest confidence.
        pats=[r"(?:tloušťka|tloustka|thickness|dicke|dicke|höhe|hoehe|výška|vyska)\s*[:=]?\s*(\d+(?:[.,]\d+)?)\s*mm",
              r"(\d+(?:[.,]\d+)?)\s*mm\s*(?:tloušťka|tloustka|thickness|dicke|höhe|hoehe|výška|vyska)"]
        for pat in pats:
            m=re.search(pat,text,re.I)
            if m:
                val=float(m.group(1).replace(",","."))
                if 0.1<=val<=500: hints["height"]=val; break

        # Typical drawing title block: "6 x 56 x 54" / "6 × 56 × 54".
        # We treat the smallest stock dimension only as a thickness hint.
        dim_matches=re.findall(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)(?!\d)",text,re.I)
        if dim_matches:
            dims=[float(v.replace(",",".")) for v in dim_matches[0]]
            if all(0.1<=v<=5000 for v in dims):
                hints["stock_dims"]=dims
                if hints["height"] is None:
                    hints["height"]=min(dims)
                    hints["warnings"].append("Výška z PDF je odvozena z nejmenšího rozměru polotovaru – potvrďte.")
    except Exception as e:
        hints["warnings"].append(f"PDF se nepodařilo přečíst: {e}")
    return hints





def normalize_material_final_mvp(current, text):
    cur=str(current or "")
    if cur and cur.lower() not in ("nezjištěn","nezjisteno","none",""): return current
    low=(text or "").lower().replace(",",".")
    if re.search(r"\b1\.4034\b",low): return "Nerez 1.4034"
    if re.search(r"\b1\.4305\b",low): return "Nerez 1.4305"
    if re.search(r"\b(tool\s*steel|werkzeugstahl|nástrojov[áa]\s*ocel)\b",low,re.I): return "Nástrojová ocel"
    if re.search(r"\b(stainless\s*steel|stainless|edelstahl|nerez)\b",low,re.I): return "Nerezová ocel"
    if re.search(r"\b(mild\s*steel|low\s*carbon\s*steel)\b",low,re.I): return "Běžná ocel"
    return current

def extract_final_mvp_facts(text):
    s=(text or "").replace("×","x").replace("⌀","Ø").replace(",",".")
    out={"height_candidates":[],"pitch_notes":[],"hardness":None}
    m=re.search(r"\bHRC\s*(\d{1,2})\s*[-–]\s*(\d{1,2})\b",s,re.I)
    if m: out["hardness"]=f"HRC {m.group(1)}–{m.group(2)}"
    for m in re.finditer(r"\b(\d{1,3})\s*[xX]\s*(\d+(?:\.\d+)?)\s*=\s*(\d+(?:\.\d+)?)\b",s):
        note=f"{m.group(1)}×{m.group(2)}={m.group(3)}"
        if note not in out["pitch_notes"]: out["pitch_notes"].append(note)
    for pat in [r"(?:thickness|thk|cut\s*height|height|tloušťka|tloustka|výška\s*řezu|vyska\s*rezu|dicke|stärke)\s*[:=]?\s*(\d+(?:\.\d+)?)\s*mm",r"\b(\d+(?:\.\d+)?)\s*mm\s*(?:thick|thickness)\b"]:
        for m in re.finditer(pat,s,re.I):
            try:
                v=float(m.group(1))
                if 0<v<=500 and v not in out["height_candidates"]: out["height_candidates"].append(v)
            except: pass
    return out

def infer_edm_geometry_types(text, tolerances=None):
    """Suggest geometry types only; never calculate cutting path from PDF pixels."""
    s=(text or "").replace("⌀","Ø").replace("φ","Ø").replace(",",".")
    low=s.lower(); found=[]
    def add(label,reason,confidence="k potvrzení"):
        if not any(x["label"]==label for x in found):
            found.append({"label":label,"reason":reason,"confidence":confidence})
    if re.search(r"(internal\s*(?:gear|spline)|internal\s*teeth|innenverzahnung|vnitřní\s*ozuben|vnitrni\s*ozuben|drážkování|drazkovani)",low,re.I):
        add("Vnitřní ozubení / členitý vnitřní profil","explicitní označení ozubení/drážkování","vysoká")
    if re.search(r"(keyway|slot|drážk|drazk)",low,re.I):
        add("Drážka","explicitní označení drážky","vysoká")
    radii=[]
    for m in re.finditer(r"\bR\s*(\d+(?:\.\d+)?)",s,re.I):
        try:radii.append(float(m.group(1)))
        except:pass
    u=sorted(set(radii))
    for a,b in zip(u,u[1:]):
        if a>3 and 0.3 <= b-a <= 8:
            add("Vnitřní ozubení / členitý vnitřní profil",
                f"blízké rádiusy R{a:g} a R{b:g} mohou popisovat hlavový/patní rádius členitého profilu")
            break
    fits=" ".join(tolerances or [])
    if re.search(r"\d+(?:\.\d+)?\s*(?:H6|H7|H8|H9)\b",fits,re.I):
        add("Přesný otvor / vnitřní profil","přesně tolerovaný otvor")
    for lab in ["Vnitřní profil","Vnější obrys","Více profilů","Jiná část"]:
        add(lab,"ruční možnost","ručně")
    return found

def visual_pdf_text(upload):
    """macOS Vision OCR fallback for image-only PDFs.
    Returns OCR text only; never derives an EDM path from pixels.
    """
    try:
        import fitz, tempfile, os
        from PIL import Image
        from ocrmac import ocrmac
        doc=fitz.open(stream=upload.getvalue(), filetype="pdf")
        chunks=[]
        # MVP: first two pages are enough for drawing/title-block extraction.
        for page in list(doc)[:2]:
            pix=page.get_pixmap(matrix=fitz.Matrix(2.2,2.2), alpha=False)
            png=pix.tobytes("png")
            with tempfile.NamedTemporaryFile(suffix=".png",delete=False) as f:
                f.write(png); tmp=f.name
            try:
                rows=ocrmac.OCR(tmp, language_preference=["en-US","de-DE"]).recognize()
                for row in rows or []:
                    # ocrmac rows are normally (text, confidence, bbox)
                    if isinstance(row,(list,tuple)) and row:
                        chunks.append(str(row[0]))
                    elif isinstance(row,str):
                        chunks.append(row)
            finally:
                try: os.unlink(tmp)
                except: pass
        return "\n".join(chunks)
    except Exception:
        return ""

def _pdf_text_with_visual_fallback(upload):
    """Return (text, used_visual_ocr)."""
    try:
        import fitz
        doc=fitz.open(stream=upload.getvalue(), filetype="pdf")
        text="\n".join(p.get_text("text") for p in doc)
    except Exception:
        text=""
    if len(re.sub(r"\s+","",text)) >= 20:
        return text, False
    ocr=visual_pdf_text(upload)
    return (ocr, True) if len(re.sub(r"\s+","",ocr)) >= 20 else (text, False)

def pdf_only_assistant(upload):
    """Extract reviewable manufacturing hints from a PDF. Never claims an EDM path from raster/vector drawing alone."""
    out={"preview":None,"part":None,"qty":None,"features":[],"dims":[],"text":""}
    try:
        import fitz, re
        doc=fitz.open(stream=upload.getvalue(), filetype="pdf")
        if len(doc):
            page=doc[0]
            pix=page.get_pixmap(matrix=fitz.Matrix(1.35,1.35), alpha=False)
            out["preview"]=pix.tobytes("png")
        text, used_visual_ocr = _pdf_text_with_visual_fallback(upload)
        out["text"]=text
        out["visual_ocr"]=used_visual_ocr
        # part number: prefer drawing-number-like tokens with letters/digits/hyphen/dot
        
        # Drawing / part number: first prefer explicit drawing-number fields, then filename, then generic tokens.
        explicit = re.search(r"(?:Z\.-?Nr\.|Drawing\s*(?:No\.?|number)|Výkres|Vykres)\s*[:#-]?\s*([A-Z0-9][A-Z0-9._/-]{3,24})", text, re.I)
        if explicit:
            out["part"] = explicit.group(1).strip(".-_/ ")
        if not out["part"]:
            stem = re.sub(r"\.pdf$", "", getattr(upload, "name", ""), flags=re.I).strip()
            if stem and not re.fullmatch(r"(?i)(drawing|vykres|výkres|scan|document)", stem):
                out["part"] = stem
        candidates=re.findall(r"\b[A-Z0-9][A-Z0-9._/-]{3,24}\b", text, re.I)
        for cand in candidates:
            if out["part"]: break
            c=cand.strip(".-_/ ")
            if re.fullmatch(r"\d{1,3}[-–]\d{1,3}", c):
                continue
            if re.fullmatch(r"\d+(?:[.,]\d+)?", c):
                continue
            # Require a genuinely drawing-number-like token: a letter, or >=3 digits on one side of a separator.
            if re.search(r"[A-Z]", c, re.I) or re.search(r"\d{3,}[-._/]\d+|\d+[-._/]\d{3,}", c):
                # Do not take tokens immediately following HRC / hardness.
                pos=text.find(c)
                ctx=text[max(0,pos-18):pos].lower() if pos>=0 else ""
                if "hrc" not in ctx and "hardness" not in ctx:
                    out["part"]=c; break
        m=re.search(r"Qty\.?\s*[-:]?\s*(\d+)\s*(?:pc|pcs|ks)?",text,re.I)
        if m: out["qty"]=int(m.group(1))
        # Toleranced diameters and linear dimensions. These are candidates, not proof of EDM.
        pats=[
            (r"(?:Ø|⌀|@|[O0](?=\s*\d)|P)\s*(\d+(?:[.,]\d+)?)\s*(H6|H7|P9|g6|f8|h6|n6|N6|H8|H9|JS\d+)", "tolerovaný průměr"),
            (r"(?<![Ø⌀P\d])(\d+(?:[.,]\d+)?)\s*(P9|H6|H7|H8|H9|g6|f8|h6|n6|N6|JS\d+)\b", "tolerovaný rozměr"),
        ]
        seen=set()
        for pat,kind in pats:
            for mm in re.finditer(pat,text,re.I):
                val=mm.group(1).replace(",","."); tol=mm.group(2)
                label=f"{val} {tol}"
                if kind=="tolerovaný průměr": label="Ø"+label
                key=label.lower()
                if key not in seen:
                    seen.add(key); out["features"].append({"label":label,"kind":kind,"confidence":"k potvrzení"})
        # Plain dimensions for context only.
        for mm in re.finditer(r"(?<![\d.,])(\d+(?:[.,]\d+)?)(?!\s*(?:H7|P9|g6|h6))",text):
            try:
                v=float(mm.group(1).replace(",","."))
                if 1<=v<=1000 and v not in out["dims"]: out["dims"].append(v)
            except: pass
        out["dims"]=out["dims"][:12]
    except Exception:
        pass
    return out



def general_drawing_parser(upload, base=None):
    """General, conservative parser for vector/text PDFs.
    Returns structured drawing facts and EDM candidates; no candidate is auto-confirmed.
    Raster/image-only PDFs are explicitly flagged for vision/manual review.
    """
    out={"text_layer":False,"material":None,"stock":None,"part":None,"qty":None,
         "dimensions":[],"tolerances":[],"surface":[],"process_notes":[],"hardness":None,
         "edm_candidates":[],"height_candidates":[],"warnings":[]}
    try:
        import fitz
        doc=fitz.open(stream=upload.getvalue(), filetype="pdf")
        native_text="\n".join(p.get_text("text") for p in doc)
        out["text_layer"]=len(re.sub(r"\s+","",native_text))>=20
        text=native_text
        out["visual_ocr"]=False
        if not out["text_layer"]:
            ocr_text=visual_pdf_text(upload)
            if len(re.sub(r"\s+","",ocr_text))>=20:
                text=ocr_text
                out["visual_ocr"]=True
                out["warnings"].append("PDF je obrazové. Údaje níže byly přečteny vizuálně (OCR) a je potřeba je potvrdit.")
            else:
                out["warnings"].append("PDF nemá použitelnou textovou vrstvu a vizuální čtení se nepodařilo. Použijte ruční vstup nebo CAD.")
                return out
        norm=text.replace("⌀","Ø").replace("φ","Ø").replace(",",".")
        out["analysis_text"]=text
        hints=base or extract_pdf_hints(upload)
        out["material"]=hints.get("material")
        if not out["material"]:
            mm=re.search(r"(?:Material|Werkstoff)\s*[:=-]?\s*([^\n\r]{2,50})", text, re.I)
            if mm:
                raw=mm.group(1).strip(" :-\t")
                # Reject title-block labels such as "alt", "neu", "siehe...".
                if raw and not re.fullmatch(r"(?i)(alt|neu|old|new|siehe|see|n/?a|[-–—]+)", raw):
                    if re.search(r"Tool\s*Steel",raw,re.I): out["material"]="Nástrojová ocel"
                    elif re.search(r"Mild\s*Steel",raw,re.I): out["material"]="Běžná ocel"
                    else: out["material"]=raw
        hm=re.search(r"HRC\s*(\d+(?:\s*[-–]\s*\d+)?)", text, re.I)
        if hm: out["hardness"]="HRC "+hm.group(1)
        out["stock"]=hints.get("stock_dims")
        pa=pdf_only_assistant(upload)
        out["part"]=pa.get("part"); out["qty"]=pa.get("qty")
        # Explicit tolerances / fits, including +/- and unilateral tolerances.
        patterns=[
          r"(?:Ø|@|[O0](?=\s*\d))?\s*\d+(?:\.\d+)?\s*(?:H\d+|P\d+|g\d+|f\d+|h\d+|n\d+|N\d+|JS\d+)",
          r"(?:Ø\s*)?\d+(?:\.\d+)?\s*[±]\s*\d+(?:\.\d+)?",
          r"(?:Ø\s*)?\d+(?:\.\d+)?\s*\+\s*\d+(?:\.\d+)?\s*(?:/|\n|\s)+\s*-?\s*0(?:\.0+)?",
          r"(?:R|Ø)\s*\d+(?:\.\d+)?(?:\s*[±]\s*\d+(?:\.\d+)?)?"
        ]
        vals=[]
        for pat in patterns:
            vals += [re.sub(r"\s+"," ",m.group(0)).strip() for m in re.finditer(pat,norm,re.I)]
        general_tols = re.findall(r"DIN\s*ISO\s*2768\s*[- ]?[A-Za-z]{1,3}", text, re.I)
        vals = general_tols + vals
        out["tolerances"]=list(dict.fromkeys(vals))[:30]
        # Surface finish and process notes.
        out["surface"]=list(dict.fromkeys(re.findall(r"\bRa\s*\d+(?:\.\d+)?",norm,re.I)))[:12]
        notes=[]
        for key in ["Laserschnitt","laser","gravieren","geschliffen","poliert","Härten","Schneidspalt","Außenkontur","Aussenkontur","Konturen"]:
            if re.search(key,norm,re.I): notes.append(key)
        out["process_notes"]=notes
        # Candidate generation is evidence-based, not a claim that EDM is required.
        for tol in out["tolerances"]:
            score=0; reasons=[]
            if re.search(r"\b(?:H\d+|P\d+|g\d+|f\d+|h\d+|n\d+|N\d+|JS\d+)\b",tol,re.I): score+=2; reasons.append("přesná tolerance/uložení")
            if "Ø" in tol: reasons.append("průměr/otvor")
            if re.search(r"±\s*0\.0(?:0?\d)",tol): score+=2; reasons.append("těsná tolerance")
            if score:
                out["edm_candidates"].append({"label":tol,"score":score,"reason":", ".join(reasons)})
        # Drawing-level evidence: tight contour tolerance / cutting gap can indicate tooling geometry.
        if re.search(r"0\.005",norm) and re.search(r"kontur",norm,re.I):
            out["edm_candidates"].append({"label":"Přesná kontura ±0,005 mm","score":4,"reason":"výkres uvádí velmi těsnou toleranci kontury"})
        if re.search(r"Schneidspalt",norm,re.I):
            out["edm_candidates"].append({"label":"Řezná / střižná kontura","score":3,"reason":"výkres obsahuje Schneidspalt"})
        if re.search(r"Laserschnitt",norm,re.I):
            out["warnings"].append("Výkres výslovně uvádí Laserschnitt. Tyto kontury se nesmí automaticky označit jako Wire EDM.")
        # Generic dimensions, context only.
        nums=[]
        # Never treat hardness ranges (e.g. HRC 41–48) as dimensional candidates.
        dim_norm=re.sub(r"HRC\s*\d+(?:\s*[-–]\s*\d+)?", " ", norm, flags=re.I)
        for m in re.finditer(r"(?<![\d.])(\d+(?:\.\d+)?)(?![\d.])",dim_norm):
            try:
                v=float(m.group(1))
                if 0.01<=v<=5000: nums.append(v)
            except: pass
        out["dimensions"]=list(dict.fromkeys(nums))[:30]
        # Possible cut heights: conservative shortlist only. Never auto-confirm from a generic dimension.
        # Prefer explicit thickness/height; otherwise offer plausible linear dimensions for human confirmation.
        if hints.get("height"):
            out["height_candidates"].append({"value":float(hints["height"]),"reason":"výslovná tloušťka/výška nebo rozměr polotovaru"})
        else:
            plausible=[]
            for v in out["dimensions"]:
                if 1.0 <= v <= 300 and v not in plausible:
                    plausible.append(v)
            # Keep the UI useful rather than dumping every number from the drawing.
            for v in plausible[:10]:
                out["height_candidates"].append({"value":float(v),"reason":"kóta z výkresu – potvrdit, zda je ve směru drátu"})
        # Extra EDM intent evidence. These are suggestions only.
        low=norm.lower()
        intent_words=[("innenkontur","vnitřní kontura"),("innenprofil","vnitřní profil"),("nut","drážka"),("keyway","drážka"),("schneidkontur","řezná kontura"),("schneidmatrize","střižnice/nástroj")]
        for key,label in intent_words:
            if key in low and not any(c.get("label")==label for c in out["edm_candidates"]):
                out["edm_candidates"].append({"label":label,"score":2,"reason":f"výkres obsahuje výraz {key}; technolog musí potvrdit Wire EDM"})
    except Exception as e:
        out["warnings"].append(f"General parser: {e}")
    return out


def pdf_vector_components(upload, snap_tol=1.2):
    """Extract connected vector path components from page 1 of a PDF.
    Coordinates stay in PDF points. They are safe for highlighting, but are NOT mm.
    """
    import fitz
    doc=fitz.open(stream=upload.getvalue(), filetype="pdf")
    if not len(doc): return None, []
    page=doc[0]
    pix=page.get_pixmap(matrix=fitz.Matrix(1.35,1.35), alpha=False)
    preview=pix.tobytes("png")
    segs=[]
    def pt(p): return (float(p.x), float(p.y))
    for d in page.get_drawings():
        for it in d.get("items",[]):
            typ=it[0]
            try:
                if typ=="l":
                    a,b=pt(it[1]),pt(it[2]); segs.append([a,b])
                elif typ=="re":
                    r=it[1]; a=(r.x0,r.y0); b=(r.x1,r.y0); c=(r.x1,r.y1); e=(r.x0,r.y1)
                    segs += [[a,b],[b,c],[c,e],[e,a]]
                elif typ=="qu":
                    q=it[1]; pts=[pt(q.ul),pt(q.ur),pt(q.lr),pt(q.ll),pt(q.ul)]
                    segs += [[pts[i],pts[i+1]] for i in range(4)]
                elif typ=="c":
                    # Cubic Bezier: sample enough points for visual selection / approximate PDF-point length.
                    p0,p1,p2,p3=map(pt,it[1:5]); pts=[]
                    for k in range(17):
                        t=k/16; u=1-t
                        x=u**3*p0[0]+3*u*u*t*p1[0]+3*u*t*t*p2[0]+t**3*p3[0]
                        y=u**3*p0[1]+3*u*u*t*p1[1]+3*u*t*t*p2[1]+t**3*p3[1]
                        pts.append((x,y))
                    segs.append(pts)
            except Exception:
                pass
    if not segs: return preview, []
    def snap(p): return (round(p[0]/snap_tol)*snap_tol, round(p[1]/snap_tol)*snap_tol)
    # Build components without adding a dependency beyond networkx already used by the app.
    import networkx as nx
    G=nx.Graph()
    for i,pts in enumerate(segs):
        if len(pts)<2: continue
        G.add_edge(snap(pts[0]),snap(pts[-1]),idx=i)
    comps=[]
    for nodes in nx.connected_components(G):
        ids=set()
        for n in nodes:
            for _,_,data in G.edges(n,data=True): ids.add(data['idx'])
        coords=[]; L=0.0
        for i in sorted(ids):
            pts=segs[i]; coords.append(pts)
            L += sum(math.hypot(pts[j+1][0]-pts[j][0],pts[j+1][1]-pts[j][1]) for j in range(len(pts)-1))
        xs=[p[0] for line in coords for p in line]; ys=[p[1] for line in coords for p in line]
        if not xs: continue
        w=max(xs)-min(xs); h=max(ys)-min(ys)
        # Suppress tiny text strokes and whole-page frames/title blocks.
        if L<8 or (w>0.88*page.rect.width and h>0.70*page.rect.height): continue
        comps.append({'lines':coords,'pdf_length':L,'bounds':(min(xs),min(ys),max(xs),max(ys))})
    comps.sort(key=lambda c:c['pdf_length'], reverse=True)
    return preview, comps[:80]

def pdf_raster_components(preview_bytes):
    """Find real part outlines in a technical drawing.
    First suppress coloured dimension/centre/annotation lines and keep neutral dark
    geometry. This avoids the v0.8.4 failure where cyan dimension lines formed a
    fake rectangle around the clicked area. Falls back to grayscale only when the
    drawing genuinely has no usable dark geometry.
    """
    try:
        import cv2, numpy as np
        from PIL import Image
        img=np.array(Image.open(io.BytesIO(preview_bytes)).convert("RGB"))
        H,W=img.shape[:2]

        # CAD PDFs often use black for the part and cyan/red/green for dimensions.
        # Keep only dark, nearly neutral pixels. Anti-aliased black remains neutral.
        mx=img.max(axis=2).astype(np.int16)
        mn=img.min(axis=2).astype(np.int16)
        neutral_dark=((mx-mn) < 34) & (mx < 165)
        bw=(neutral_dark.astype(np.uint8)*255)
        bw=cv2.morphologyEx(bw,cv2.MORPH_CLOSE,np.ones((2,2),np.uint8))

        def extract(mask, colour_filtered=True):
            contours,hier=cv2.findContours(mask,cv2.RETR_TREE,cv2.CHAIN_APPROX_NONE)
            if hier is None: return []
            hier=hier[0]; out=[]
            for idx,cnt in enumerate(contours):
                x,y,w,h=cv2.boundingRect(cnt)
                per=float(cv2.arcLength(cnt,True)); area=float(abs(cv2.contourArea(cnt)))
                if w<22 or h<14 or per<55 or area<90: continue
                if w>0.78*W or h>0.78*H: continue
                ar=max(w/max(h,1),h/max(w,1))
                if ar>12: continue
                fill=area/max(1.0,w*h)
                if fill<0.035: continue
                size_frac=(w*h)/(W*H)
                if size_frac>0.16: continue
                eps=max(0.55,0.0012*per)
                approx=cv2.approxPolyDP(cnt,eps,True)[:,0,:].astype(float)
                if len(approx)<5: continue
                pts=[(float(a),float(b)) for a,b in approx]
                if pts[0]!=pts[-1]: pts.append(pts[0])
                parent=int(hier[idx][3]); child=int(hier[idx][2])
                score=0.0
                if 0.00015<=size_frac<=0.06: score+=4.0
                if parent>=0: score+=1.0
                if child>=0: score+=0.6
                if 0.12<=fill<=0.9: score+=1.0
                if colour_filtered: score+=2.0
                out.append({'lines':[pts],'pdf_length':per,
                            'bounds':(float(x),float(y),float(x+w),float(y+h)),
                            'raster':True,'score':score,'contour_id':idx,
                            'source':'dark_geometry' if colour_filtered else 'grayscale'})
            out.sort(key=lambda c:(c.get('score',0),c['pdf_length']),reverse=True)
            keep=[]
            for c in out:
                b=c['bounds']; dup=False
                for k in keep:
                    kb=k['bounds']
                    if sum(abs(b[i]-kb[i]) for i in range(4))<14:
                        dup=True; break
                if not dup: keep.append(c)
                if len(keep)>=80: break
            return keep

        result=extract(bw,True)
        # Conservative fallback for monochrome drawings.
        if len(result)<2:
            gray=cv2.cvtColor(img,cv2.COLOR_RGB2GRAY)
            gray=cv2.GaussianBlur(gray,(3,3),0)
            fallback=cv2.threshold(gray,155,255,cv2.THRESH_BINARY_INV)[1]
            result=extract(fallback,False)
        return result
    except Exception:
        return []

def pdf_component_plot(preview_bytes, comps, selected=None):
    """Simple PDF picker: click INSIDE a closed profile, not on its thin edge.
    Dense invisible hit-points fill the interior of each contour. After selection,
    the chosen contour is highlighted orange and the view zooms to it.
    """
    import plotly.graph_objects as go, base64
    import numpy as np
    from PIL import Image
    img=Image.open(io.BytesIO(preview_bytes)); W,H=img.size
    scale=1.0 if (comps and comps[0].get('raster')) else 1.35
    fig=go.Figure()

    # Smaller profiles first so an inner hole wins over a larger surrounding outline.
    order=sorted(range(len(comps)), key=lambda i:(comps[i]['bounds'][2]-comps[i]['bounds'][0])*(comps[i]['bounds'][3]-comps[i]['bounds'][1]))
    for i in order:
        c=comps[i]
        line=c['lines'][0] if c.get('lines') else []
        if len(line)<4: continue
        pts=np.asarray(line, dtype=float)
        x0,y0,x1,y1=c['bounds']

        # Fill the profile with invisible clickable points. This makes the intended
        # interaction "click inside the hole/profile" instead of "hit a 1 px line".
        hx=[]; hy=[]
        try:
            import cv2
            poly=pts.astype(np.float32).reshape((-1,1,2))
            spacing=max(6.0, min(14.0, min(x1-x0,y1-y0)/5.0))
            xs=np.arange(x0+spacing/2, x1, spacing)
            ys=np.arange(y0+spacing/2, y1, spacing)
            for yy in ys:
                for xx in xs:
                    if cv2.pointPolygonTest(poly,(float(xx),float(yy)),False)>=0:
                        hx.append(float(xx)*scale); hy.append(float(yy)*scale)
            # Always include centroid-ish point as a generous target.
            M=cv2.moments(poly)
            if abs(M.get('m00',0))>1e-9:
                hx.append(float(M['m10']/M['m00'])*scale)
                hy.append(float(M['m01']/M['m00'])*scale)
        except Exception:
            hx=[(x0+x1)*0.5*scale]; hy=[(y0+y1)*0.5*scale]

        if hx:
            fig.add_trace(go.Scatter(
                x=hx,y=hy,mode='markers',
                marker=dict(size=20,opacity=0.012),
                customdata=[i]*len(hx),
                hovertemplate='Klikni sem – vybrat tento profil<extra></extra>',
                showlegend=False,name=f'Profil {i+1}'))

    # Draw selection last and clearly.
    if selected is not None and 0 <= selected < len(comps):
        c=comps[selected]
        for line in c['lines']:
            fig.add_trace(go.Scatter(
                x=[p[0]*scale for p in line],y=[p[1]*scale for p in line],
                mode='lines',line=dict(width=7,color='#ff8c00'),
                hoverinfo='skip',showlegend=False))

    b64=base64.b64encode(preview_bytes).decode()
    fig.add_layout_image(dict(source='data:image/png;base64,'+b64,x=0,y=0,sizex=W,sizey=H,
                              xref='x',yref='y',xanchor='left',yanchor='top',layer='below'))

    if selected is not None and 0 <= selected < len(comps):
        x0,y0,x1,y1=comps[selected]['bounds']
        pad=max(35.0,0.65*max(x1-x0,y1-y0))
        xr=[max(0,x0-pad),min(W,x1+pad)]
        yr=[min(H,y1+pad),max(0,y0-pad)]
        fig.update_xaxes(range=xr,visible=False)
        fig.update_yaxes(range=yr,visible=False,scaleanchor='x',scaleratio=1)
    else:
        fig.update_xaxes(range=[0,W],visible=False)
        fig.update_yaxes(range=[H,0],visible=False,scaleanchor='x',scaleratio=1)
    fig.update_layout(height=820,margin=dict(l=0,r=0,t=48,b=0),dragmode=False,
                      title='Klikni DOVNITŘ otvoru nebo profilu, který se má řezat')
    return fig

def reconstruct_pdf_keyway(text: str):
    """Conservative PDF-only reconstruction for a straight internal keyway/slot in a bore.

    Requires: bore diameter with H tolerance, slot width with P9, outer diameter,
    and an overall top-to-slot-bottom dimension. Returns a proposal only when the
    dimensions form a geometrically consistent profile. Human confirmation remains required.
    """
    t=text.replace(",", ".")
    def first(patterns):
        for pat in patterns:
            m=re.search(pat,t,re.I)
            if m:
                try: return float(m.group(1))
                except: pass
        return None
    bore=first([r"(?:Ø|⌀|P)\s*(\d+(?:\.\d+)?)\s*H7\b", r"\b(\d+(?:\.\d+)?)\s*H7\b"])
    slot=first([r"(?<![Ø⌀P\d])(\d+(?:\.\d+)?)\s*P9\b"])
    outer=first([r"(?:Ø|⌀|P)\s*(\d+(?:\.\d+)?)\s*g6\b"])
    if not (bore and slot and outer): return None
    # Candidate vertical dimensions from the drawing. We need top of OD -> slot bottom.
    nums=[]
    for m in re.finditer(r"(?<![\d.])(\d{2}(?:\.\d+)?)(?![\d.])",t):
        try:
            v=float(m.group(1))
            if bore < v < outer: nums.append(v)
        except: pass
    nums=sorted(set(nums), reverse=True)
    r=bore/2.0; half=slot/2.0
    if half>=r: return None
    yi=math.sqrt(r*r-half*half)
    # bottom intersection is at -yi; OD top is +outer/2. Slot bottom must be below it.
    valid=[]
    for overall in nums:
        bottom=outer/2.0-overall
        if bottom < -yi-0.05:
            side=(-yi)-bottom
            removed_arc=2*r*math.asin(half/r)
            path=2*side+slot
            if 0 < side < outer:
                valid.append((overall,bottom,side,path,removed_arc))
    if not valid: return None
    # Prefer the smallest consistent top-to-bottom dimension; avoids unrelated larger dims.
    overall,bottom,side,path,removed_arc=sorted(valid,key=lambda x:x[0])[0]
    return {"bore":bore,"slot":slot,"outer":outer,"overall":overall,
            "side":side,"path":path,"removed_arc":removed_arc,
            "confidence":"návrh k potvrzení"}

def step_height(upload):
    """Read STEP with OpenCascade (OCP) and return smallest bounding-box dimension as a height suggestion."""
    suffix=Path(upload.name).suffix.lower() or ".step"
    with tempfile.NamedTemporaryFile(delete=False,suffix=suffix) as f:
        f.write(upload.getvalue()); fn=f.name
    try:
        try:
            from OCP.STEPControl import STEPControl_Reader
            from OCP.IFSelect import IFSelect_RetDone
            from OCP.Bnd import Bnd_Box
            from OCP.BRepBndLib import BRepBndLib
            reader=STEPControl_Reader()
            status=reader.ReadFile(fn)
            if status != IFSelect_RetDone:
                return None,None,None
            reader.TransferRoots()
            shape=reader.OneShape()
            box=Bnd_Box()
            BRepBndLib.Add_s(shape,box)
            xmin,ymin,zmin,xmax,ymax,zmax=box.Get()
            dims=[abs(xmax-xmin),abs(ymax-ymin),abs(zmax-zmin)]
            dims=[round(x,4) for x in dims]
            positive=sorted(x for x in dims if x>0.01)
            if positive:
                return positive[0],dims,"STEP / OpenCascade bounding box – potvrďte orientaci řezu"
        except Exception:
            pass

        # Last-resort STEP text fallback: useful for simple prismatic models.
        # It is intentionally only a suggestion, never a confirmed machining height.
        try:
            txt=Path(fn).read_text(errors="ignore")
            pts=re.findall(r"CARTESIAN_POINT\s*\([^,]*,\s*\(\s*([-+0-9.Ee]+)\s*,\s*([-+0-9.Ee]+)\s*,\s*([-+0-9.Ee]+)\s*\)\s*\)",txt,re.I)
            if pts:
                vals=[[float(a),float(b),float(c)] for a,b,c in pts]
                dims=[max(p[i] for p in vals)-min(p[i] for p in vals) for i in range(3)]
                positive=sorted(x for x in dims if 0.01<x<10000)
                if positive:
                    return positive[0],dims,"STEP text fallback – nízká jistota, potvrďte"
        except Exception:
            pass
    finally:
        try: os.unlink(fn)
        except: pass
    return None,None,None



def step_wire_candidates(upload):
    """Robust STEP inner-profile finder.
    Scans ALL planar faces perpendicular to the thinnest model axis, measures every
    inner wire in native CAD units and ranks likely Wire EDM profiles. This avoids
    depending on one arbitrarily chosen 'main' face.
    """
    suffix=Path(upload.name).suffix.lower() or ".step"
    with tempfile.NamedTemporaryFile(delete=False,suffix=suffix) as f:
        f.write(upload.getvalue()); fn=f.name
    try:
        from OCP.STEPControl import STEPControl_Reader
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopAbs import TopAbs_FACE, TopAbs_WIRE, TopAbs_EDGE
        from OCP.TopoDS import TopoDS
        from OCP.BRepAdaptor import BRepAdaptor_Surface, BRepAdaptor_Curve
        from OCP.GeomAbs import GeomAbs_Plane
        from OCP.BRepTools import BRepTools
        from OCP.GProp import GProp_GProps
        from OCP.BRepGProp import BRepGProp

        reader=STEPControl_Reader()
        if reader.ReadFile(fn) != IFSelect_RetDone:
            return [], {"error":"STEP ReadFile failed"}
        reader.TransferRoots(); shape=reader.OneShape()
        box=Bnd_Box(); BRepBndLib.Add_s(shape,box)
        xmin,ymin,zmin,xmax,ymax,zmax=box.Get()
        dims=[abs(xmax-xmin),abs(ymax-ymin),abs(zmax-zmin)]
        axis=min(range(3), key=lambda i:dims[i] if dims[i]>0.01 else 1e99)

        raw=[]; face_count=0
        ex=TopExp_Explorer(shape,TopAbs_FACE)
        while ex.More():
            face=TopoDS.Face_s(ex.Current()); ad=BRepAdaptor_Surface(face)
            if ad.GetType()==GeomAbs_Plane:
                d=ad.Plane().Axis().Direction(); nv=[abs(d.X()),abs(d.Y()),abs(d.Z())]
                # End faces for the likely cutting direction.
                if nv[axis] >= 0.94:
                    face_count += 1
                    outer=BRepTools.OuterWire_s(face)
                    wx=TopExp_Explorer(face,TopAbs_WIRE)
                    while wx.More():
                        w=TopoDS.Wire_s(wx.Current())
                        if not w.IsSame(outer):
                            lp=GProp_GProps(); BRepGProp.LinearProperties_s(w,lp)
                            L=float(lp.Mass())
                            wb=Bnd_Box(); BRepBndLib.Add_s(w,wb)
                            a,b,c,d2,e,f2=wb.Get(); ext=[abs(d2-a),abs(e-b),abs(f2-c)]
                            planar=[v for i,v in enumerate(ext) if i!=axis]
                            if L>0.5 and len(planar)==2 and max(planar)>0.1:
                                sx,sy=sorted(planar,reverse=True)
                                # Keep drawable native CAD geometry for visual confirmation.
                                # Each edge is sampled independently so we never invent a connecting segment.
                                plane_axes=[i for i in range(3) if i!=axis]
                                draw_edges=[]
                                ee=TopExp_Explorer(w,TopAbs_EDGE)
                                while ee.More():
                                    edge=TopoDS.Edge_s(ee.Current())
                                    pts=[]
                                    try:
                                        ca=BRepAdaptor_Curve(edge)
                                        u0,u1=float(ca.FirstParameter()),float(ca.LastParameter())
                                        eg=GProp_GProps(); BRepGProp.LinearProperties_s(edge,eg)
                                        eL=max(float(eg.Mass()),0.01)
                                        n=max(3,min(120,int(max(8,eL*4))))
                                        for j in range(n):
                                            u=u0+(u1-u0)*j/(n-1)
                                            pp=ca.Value(u); xyz=[float(pp.X()),float(pp.Y()),float(pp.Z())]
                                            pts.append([xyz[plane_axes[0]],xyz[plane_axes[1]]])
                                    except Exception:
                                        pass
                                    if len(pts)>=2: draw_edges.append(pts)
                                    ee.Next()
                                raw.append({"length":L,"size":[sx,sy],"bbox_area":sx*sy,
                                            "aspect":sx/max(sy,1e-9),"draw_edges":draw_edges})
                        wx.Next()
            ex.Next()
        # Opposite end faces create duplicate wires. Collapse geometrically equal copies,
        # but retain repeated equal holes on one face only once as candidate TYPES.
        uniq=[]
        for c in sorted(raw,key=lambda x:(x['bbox_area'],x['length']),reverse=True):
            same=any(abs(c['length']-u['length'])<0.03 and
                     abs(c['size'][0]-u['size'][0])<0.03 and
                     abs(c['size'][1]-u['size'][1])<0.03 for u in uniq)
            if not same: uniq.append(c)
        # Prefer non-circular / elongated internal shapes (typical spline/slot) over round bores.
        for c in uniq:
            circularity_hint=abs(c['length']-(3.141592653589793*(c['size'][0]+c['size'][1])/2.0)) / max(c['length'],1e-9)
            c['nonround_score']=circularity_hint + max(0,c['aspect']-1.05)*0.2
        uniq.sort(key=lambda x:(x['nonround_score'],x['bbox_area'],x['length']),reverse=True)
        for i,c in enumerate(uniq): c['index']=i
        return uniq[:30], {"dims":dims,"axis":axis,"faces_scanned":face_count,"raw_wires":len(raw)}
    except Exception as e:
        return [], {"error":str(e)}
    finally:
        try: os.unlink(fn)
        except: pass


def step_inner_profile_plot(candidate):
    """Visual proof of the exact STEP wire used for the measured length.
    Orange geometry is the candidate itself; no PDF/raster approximation is involved.
    """
    import plotly.graph_objects as go
    edges=(candidate or {}).get("draw_edges") or []
    if not edges:
        return None
    fig=go.Figure()
    allx=[]; ally=[]
    for pts in edges:
        if len(pts)<2: continue
        xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
        allx.extend(xs); ally.extend(ys)
        fig.add_trace(go.Scatter(x=xs,y=ys,mode="lines",
            line=dict(color="#ff8c00",width=6),
            hovertemplate="Započítaná STEP řezná dráha<extra></extra>",showlegend=False))
    if not allx: return None
    fig.update_yaxes(scaleanchor="x",scaleratio=1,title="mm")
    fig.update_xaxes(title="mm")
    fig.update_layout(height=500,margin=dict(l=10,r=10,t=45,b=10),
        title="Oranžově = přesně profil zahrnutý do řezné délky",dragmode="pan")
    return fig


def step_outer_analysis(upload):
    """Measure the full outer wire and return drawable CAD geometry.
    Tooth-only geometry remains a candidate: the user must visually confirm the orange segment.
    """
    suffix=Path(upload.name).suffix.lower() or ".step"
    with tempfile.NamedTemporaryFile(delete=False,suffix=suffix) as f:
        f.write(upload.getvalue()); fn=f.name
    try:
        from OCP.STEPControl import STEPControl_Reader
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopAbs import TopAbs_FACE, TopAbs_EDGE
        from OCP.TopoDS import TopoDS
        from OCP.BRepAdaptor import BRepAdaptor_Surface, BRepAdaptor_Curve
        from OCP.GeomAbs import GeomAbs_Plane
        from OCP.BRepTools import BRepTools
        from OCP.GProp import GProp_GProps
        from OCP.BRepGProp import BRepGProp
        r=STEPControl_Reader()
        if r.ReadFile(fn)!=IFSelect_RetDone: return None
        r.TransferRoots(); shape=r.OneShape()
        box=Bnd_Box(); BRepBndLib.Add_s(shape,box); b=box.Get()
        dims=[abs(b[3]-b[0]),abs(b[4]-b[1]),abs(b[5]-b[2])]
        axis=min(range(3),key=lambda i:dims[i] if dims[i]>0.01 else 1e99)
        plane_axes=[i for i in range(3) if i!=axis]
        candidates=[]
        ex=TopExp_Explorer(shape,TopAbs_FACE)
        while ex.More():
            face=TopoDS.Face_s(ex.Current()); ad=BRepAdaptor_Surface(face)
            if ad.GetType()==GeomAbs_Plane:
                d=ad.Plane().Axis().Direction(); nv=[abs(d.X()),abs(d.Y()),abs(d.Z())]
                if nv[axis]>=0.94:
                    ow=BRepTools.OuterWire_s(face)
                    gp=GProp_GProps(); BRepGProp.LinearProperties_s(ow,gp); total=float(gp.Mass())
                    edge_data=[]
                    ee=TopExp_Explorer(ow,TopAbs_EDGE)
                    while ee.More():
                        edge=TopoDS.Edge_s(ee.Current())
                        eg=GProp_GProps(); BRepGProp.LinearProperties_s(edge,eg); L=float(eg.Mass())
                        pts=[]
                        try:
                            ca=BRepAdaptor_Curve(edge)
                            u0,u1=float(ca.FirstParameter()),float(ca.LastParameter())
                            n=max(2,min(80,int(max(6,L*2))))
                            for j in range(n):
                                u=u0+(u1-u0)*j/(n-1)
                                p=ca.Value(u); xyz=[float(p.X()),float(p.Y()),float(p.Z())]
                                pts.append([xyz[plane_axes[0]],xyz[plane_axes[1]]])
                        except Exception:
                            pass
                        edge_data.append({"length":L,"points":pts})
                        ee.Next()
                    candidates.append({"total":total,"edges":edge_data})
            ex.Next()
        if not candidates: return None
        best=max(candidates,key=lambda x:x["total"]); total=best["total"]; edge_data=best["edges"]
        edges=[e["length"] for e in edge_data]
        short=[x for x in edges if 0.15 <= x <= 3.0]
        tooth_len=None; selected=set(); dom=None
        if len(short)>=8:
            bins={}
            for x in short:
                k=round(x/0.05)*0.05; bins[k]=bins.get(k,0)+1
            dom,count=max(bins.items(),key=lambda kv:kv[1])
            if count>=8:
                tol=max(0.08,dom*0.18)
                for i,e in enumerate(edge_data):
                    if abs(e["length"]-dom)<=tol: selected.add(i)
                if 0.45 <= dom <= 0.9:
                    for i,e in enumerate(edge_data):
                        if 0.9 <= e["length"] <= 1.1: selected.add(i)
                tooth_len=sum(edge_data[i]["length"] for i in selected)
        return {"outer_length":total,"tooth_length":tooth_len,"edge_count":len(edges),
                "tooth_edge_count":len(selected),"dims":dims,"axis":axis,
                "edges":edge_data,"tooth_indices":sorted(selected),"dominant_tooth_edge":dom}
    except Exception as e:
        return {"error":str(e)}
    finally:
        try: os.unlink(fn)
        except: pass

def step_path_plot(outer_info, mode="outer"):
    """Plot the STEP outer contour; orange is exactly the geometry included in the shown length."""
    import plotly.graph_objects as go
    if not outer_info or outer_info.get("error") or not outer_info.get("edges"):
        return None
    fig=go.Figure()
    selected=set(range(len(outer_info["edges"]))) if mode=="outer" else set(outer_info.get("tooth_indices",[]))
    # Full CAD contour in subdued grey.
    for i,e in enumerate(outer_info["edges"]):
        pts=e.get("points") or []
        if len(pts)<2: continue
        fig.add_trace(go.Scatter(x=[p[0] for p in pts],y=[p[1] for p in pts],mode="lines",
                                 line=dict(color="#707070",width=2),hoverinfo="skip",showlegend=False))
    # Exact included machining path in orange.
    for i in selected:
        if i>=len(outer_info["edges"]): continue
        pts=outer_info["edges"][i].get("points") or []
        if len(pts)<2: continue
        fig.add_trace(go.Scatter(x=[p[0] for p in pts],y=[p[1] for p in pts],mode="lines",
                                 line=dict(color="#ff8c00",width=6),hovertemplate="Započítaná řezná dráha<extra></extra>",showlegend=False))
    fig.update_yaxes(scaleanchor="x",scaleratio=1,title="mm")
    fig.update_xaxes(title="mm")
    fig.update_layout(height=520,margin=dict(l=10,r=10,t=35,b=10),
                      title="Oranžově = přesně započítaná řezná dráha",
                      dragmode="pan")
    return fig

def estimate(height,path,pieces,passes=2):
    # Provisional reference updated to the verified DXF geometry: 107.4 mm, 29 mm, 2 passes, 40 min.
    # This is NOT a trained model yet; calibration DB will replace it once enough complete jobs exist.
    per_piece=40*(path/107.4)*(height/29)**.65*(passes/2)
    return per_piece*pieces


def pdf_only_contour_estimate(preview_bytes, stock_dims=None):
    """PDF-only manufacturing-contour detector.
    v0.9.4 aggressively rejects dimensions, leaders, frames and text-like shapes,
    then ranks compact/nested closed contours from the main orthographic view.
    CAD remains authoritative; PDF lengths are approximate only after scale confirmation.
    """
    out={"candidates":[],"scale":None,"scale_note":None}
    try:
        import cv2, numpy as np
        from PIL import Image
        im=np.array(Image.open(io.BytesIO(preview_bytes)).convert("RGB"))
        gray=cv2.cvtColor(im,cv2.COLOR_RGB2GRAY)
        bw=cv2.threshold(gray,155,255,cv2.THRESH_BINARY_INV)[1]
        bw=cv2.morphologyEx(bw,cv2.MORPH_OPEN,np.ones((2,2),np.uint8))
        contours,hier=cv2.findContours(bw,cv2.RETR_TREE,cv2.CHAIN_APPROX_NONE)
        H,W=gray.shape
        hierarchy=hier[0] if hier is not None else None
        raw=[]
        for i,c in enumerate(contours):
            per=float(cv2.arcLength(c,True)); area=abs(float(cv2.contourArea(c)))
            x,y,w,h=cv2.boundingRect(c)
            if per<35 or area<40 or w<8 or h<8: continue
            if w>W*.82 or h>H*.82 or w*h>W*H*.22: continue
            aspect=max(w,h)/max(1,min(w,h))
            # Dimension lines / leaders are typically very long and very thin.
            # Keep real slots, but reject the 191x14-style false candidates.
            if aspect>8.0: continue
            if min(w,h)<10 and aspect>4.0: continue
            # Drawing borders/title blocks live close to page edges.
            if (x < W*.015 or y < H*.015 or x+w > W*.985 or y+h > H*.985) and w*h>W*H*.01: continue
            approx=cv2.approxPolyDP(c,0.0018*per,True).reshape(-1,2)
            if len(approx)<4: continue
            # Filled/compactness cues: thin annotation boxes have tiny area relative to bbox.
            fill=area/max(1.0,w*h)
            parent=int(hierarchy[i][3]) if hierarchy is not None else -1
            child=int(hierarchy[i][2]) if hierarchy is not None else -1
            raw.append((i,per,area,(x,y,w,h),approx,aspect,fill,parent,child))

        dims=sorted([float(x) for x in (stock_dims or []) if float(x)>0], reverse=True)
        plan=dims[:2] if len(dims)>=2 else []
        scale=None
        if len(plan)==2:
            target_ratio=max(plan)/min(plan); best=None
            for _,per,area,(x,y,w,h),pts,aspect,fill,parent,child in raw:
                if w<80 or h<80: continue
                ratio=max(w,h)/min(w,h); err=abs(ratio-target_ratio)
                score=err + 2500/max(1,w*h)
                if best is None or score<best[0]: best=(score,w,h)
            if best and best[0]<0.35:
                _,w,h=best
                s1=max(w,h)/max(plan); s2=min(w,h)/min(plan)
                if abs(s1-s2)/max(s1,s2)<0.20:
                    scale=(s1+s2)/2.0
                    out['scale']=scale
                    out['scale_note']=f"měřítko odhadnuto z hlavního pohledu a polotovaru {plan[0]:g} × {plan[1]:g} mm"

        cand=[]
        for i,per,area,(x,y,w,h),pts,aspect,fill,parent,child in raw:
            mm=per/scale if scale else None
            if mm is not None and not (8 <= mm <= 1000): continue
            cx=x+w/2; cy=y+h/2
            # Manufacturing features are usually compact closed contours, often nested
            # inside a larger part outline. Penalize top/bottom annotation zones and
            # long shallow shapes even if they technically form a closed contour.
            compact=4*np.pi*area/max(1.0,per*per)
            vertices=len(pts)
            nested_bonus=32 if parent>=0 else 0
            # Do not assume the useful view is in the top-left: many drawings put the
            # decisive EDM profile in a lower orthographic/detail view.
            drawing_zone_bonus=18 if (W*.06 < cx < W*.86 and H*.08 < cy < H*.82) else 0
            shape_bonus=18*min(1.0,compact/0.45)
            size_bonus=min(35.0,(area**0.5)*0.30)
            thin_penalty=max(0.0,aspect-3.5)*18
            edge_penalty=28 if (cy < H*.08 or cy > H*.86) else 0
            # Tooth/spline profiles are complex but still compact. This strongly promotes
            # a real serrated circular profile over a tiny arc in an isometric view.
            tooth_like = (vertices >= 18 and aspect <= 1.8 and 0.20 <= compact <= 0.92)
            complexity_bonus=min(55.0,max(0,vertices-8)*2.0) if tooth_like else min(12.0,vertices*0.35)
            score=nested_bonus+drawing_zone_bonus+shape_bonus+size_bonus+complexity_bonus-thin_penalty-edge_penalty
            kind = "ozubený / členitý profil" if tooth_like else ("kruhový profil" if aspect < 1.35 and compact > 0.55 else "uzavřená kontura")
            cand.append({"index":i,"px_length":per,"length_mm":mm,"bounds":(x,y,w,h),
                         "points":pts.tolist(),"score":score,"nested":parent>=0,
                         "compactness":compact,"vertices":vertices,"kind":kind,"tooth_like":tooth_like})
        cand.sort(key=lambda z:z['score'],reverse=True)

        # Deduplicate nearly identical double-stroke contours from rasterization.
        kept=[]
        for c in cand:
            x,y,w,h=c['bounds']; duplicate=False
            for k in kept:
                x2,y2,w2,h2=k['bounds']
                if abs(x-x2)<=4 and abs(y-y2)<=4 and abs(w-w2)<=7 and abs(h-h2)<=7:
                    duplicate=True; break
            if not duplicate: kept.append(c)
            if len(kept)>=24: break
        out['candidates']=kept
    except Exception as e:
        out['error']=type(e).__name__
    return out

def pdf_candidate_plot(preview_bytes, candidate):
    import plotly.graph_objects as go, base64
    from PIL import Image
    img=Image.open(io.BytesIO(preview_bytes)); W,H=img.size
    fig=go.Figure()
    b64=base64.b64encode(preview_bytes).decode()
    fig.add_layout_image(dict(source='data:image/png;base64,'+b64,x=0,y=0,sizex=W,sizey=H,xref='x',yref='y',xanchor='left',yanchor='top',layer='below'))
    if candidate:
        pts=candidate.get('points') or []
        if pts:
            xs=[p[0] for p in pts]+[pts[0][0]]; ys=[p[1] for p in pts]+[pts[0][1]]
            fig.add_trace(go.Scatter(x=xs,y=ys,mode='lines',line=dict(color='#ff8c00',width=5),hoverinfo='skip',showlegend=False))
    fig.update_xaxes(range=[0,W],visible=False); fig.update_yaxes(range=[H,0],visible=False,scaleanchor='x',scaleratio=1)
    fig.update_layout(height=min(850,max(520,int(H*.72))),margin=dict(l=0,r=0,t=10,b=0))
    return fig

st.set_page_config(page_title="Tvarsteel | Orientační cena Wire EDM v0.13.1",page_icon="⚡",layout="centered")
st.title("Orientační cena drátového řezání")
st.caption("Nahrajte výrobní podklady a během chvíle získáte pilotní orientační cenu. Finální cenu vždy potvrzuje technolog.")

files=st.file_uploader("1. Nahrajte podklady (PDF + DXF/STEP)",type=["dxf","step","stp","pdf"],accept_multiple_files=True)
up=next((f for f in files if f.name.lower().endswith(".dxf")),None) if files else None
step=next((f for f in files if f.name.lower().endswith((".step",".stp"))),None) if files else None
pdf=next((f for f in files if f.name.lower().endswith(".pdf")),None) if files else None

path=0.0; auto_height=0.0; pdf_hints={}; material_guess=None
if pdf:
    pdf_hints=extract_pdf_hints(pdf)
    material_guess=pdf_hints.get("material")
if step:
    ah,dims,method=step_height(step)
    auto_height=float(ah or 0.0)

st.markdown("### 2. Zakázka")
materials=["Nezjištěno / vyberte","C55","Nerez 1.4034","1.2379","1.2343","S235 / JS235","Běžná ocel","Nástrojová ocel","Nerez 1.4305","Nerez 1.4301","Nerez","Hliník","Jiný / nevím"]
material_ui_map={"1.4034":"Nerez 1.4034","1.4305":"Nerez 1.4305","1.4301":"Nerez 1.4301","Tool Steel":"Nástrojová ocel"}
mg=material_ui_map.get(material_guess,material_guess)
mi=materials.index(mg) if mg in materials else 0
c1,c2=st.columns(2)
material=c1.selectbox("Materiál",materials,index=mi)
pieces=c2.number_input("Počet kusů",min_value=1,value=1,step=1)
height_guess=float(auto_height or (pdf_hints.get("height") if pdf_hints else 0) or 0)
height=c1.number_input("Výška řezu [mm]",min_value=0.0,value=height_guess,step=0.1)
quality=c2.selectbox("Požadavek",["Standard / nevím","Hrubý řez","Přesný rozměr"])
passes_default=1 if quality=="Hrubý řez" else 2
passes=c1.number_input("Počet řezů / průchodů",min_value=1,max_value=10,value=passes_default,step=1)

# Cutting path: CAD is authoritative; PDF-only allows a manual fallback.
manual_path = 0.0
if up:
    try:
        total,cs,raw=analyze(up)
        if len(cs)==1:
            path=float(cs[0]["length"])
            st.success(f"✓ CAD načten: řezná dráha {path:.1f} mm")
            fig=plot(raw,cs,0)
            if fig: st.plotly_chart(fig,use_container_width=True)
        elif len(cs)>1:
            labels=[f"Profil {i+1} — {c['length']:.1f} mm · cca {c['bounds'][2]-c['bounds'][0]:.1f} × {c['bounds'][3]-c['bounds'][1]:.1f} mm" for i,c in enumerate(cs)]
            choice=st.selectbox("Který profil se má řezat?",list(range(len(cs))),format_func=lambda i:labels[i])
            path=float(cs[choice]["length"])
            st.caption(f"Vybraná řezná dráha: {path:.1f} mm")
            fig=plot(raw,cs,choice)
            if fig: st.plotly_chart(fig,use_container_width=True)
        else:
            st.warning("Z CADu se nepodařilo bezpečně určit řeznou konturu. Řeznou dráhu můžete zadat ručně níže.")
    except Exception:
        st.warning("DXF se nepodařilo bezpečně analyzovat. Řeznou dráhu můžete zadat ručně níže.")
elif step:
    try:
        profiles,meta=step_wire_candidates(step)
        outer=step_outer_analysis(step)
        opts=[]
        for i,c in enumerate(profiles or []):
            opts.append({"label":f"Vnitřní profil {i+1} — {c['length']:.1f} mm","length":float(c['length']),"kind":"inner","candidate":c})
        if outer and not outer.get("error") and outer.get("outer_length"):
            opts.append({"label":f"Celý vnější obvod — {outer['outer_length']:.1f} mm","length":float(outer['outer_length']),"kind":"outer"})
        if outer and not outer.get("error") and outer.get("tooth_length"):
            opts.append({"label":f"Ozubení / část vnějšího obvodu — {outer['tooth_length']:.1f} mm","length":float(outer['tooth_length']),"kind":"tooth"})
        if len(opts)>=1:
            idx=0 if len(opts)==1 else st.selectbox("Co se má řezat?",list(range(len(opts))),format_func=lambda i:opts[i]["label"])
            chosen=opts[idx]
            path=chosen["length"]
            if len(opts)==1: st.success(f"✓ CAD načten: řezná dráha {path:.1f} mm")
            else: st.caption(f"Vybraná řezná dráha: {path:.1f} mm")
            if chosen["kind"]=="inner": fig=step_inner_profile_plot(chosen["candidate"])
            elif chosen["kind"]=="outer": fig=step_path_plot(outer,"outer")
            else: fig=step_path_plot(outer,"tooth")
            if fig: st.plotly_chart(fig,use_container_width=True)
            st.caption("Oranžově je zvýrazněna geometrie, kterou kalkulačka započítává do ceny.")
        else:
            st.warning("Ze STEP se nepodařilo bezpečně určit řeznou konturu. Řeznou dráhu můžete zadat ručně níže.")
    except Exception:
        st.warning("STEP se nepodařilo bezpečně analyzovat. Řeznou dráhu můžete zadat ručně níže.")

# Manual path is essential for PDF-only jobs and also serves as a safe correction for CAD.
with st.expander("✏️ Ručně zadat / upravit řeznou dráhu", expanded=(pdf is not None and not up and not step)):
    st.caption("Použijte, pokud máte pouze PDF nebo chcete opravit automaticky zjištěnou délku.")
    manual_path=st.number_input("Řezná dráha [mm]",min_value=0.0,value=float(path),step=1.0,key="manual_customer_path")
    if manual_path>0:
        path=float(manual_path)
        st.success(f"Použitá řezná dráha: {path:.1f} mm")
    elif pdf and not up and not step:
        st.info("Zadejte délku řezné dráhy v mm. Samotný PDF výkres u složitých profilů přesnou délku bezpečně neurčí.")

st.markdown("### 3. Orientační cena")
ready=path>0 and height>0 and material!="Nezjištěno / vyberte"
if ready:
    cut=estimate(height,path,pieces,passes)
    total=cut+3
    # Pilot customer price: pure time × hourly rate range.
    # No artificial 800 Kč minimum — Tvarsteel currently prices primarily by machine hours.
    hourly_low=800.0
    hourly_high=1100.0
    lo=round((total/60*hourly_low)/10)*10
    hi=round((total/60*hourly_high)/10)*10
    price=(lo+hi)/2
    st.success("Podklady obsahují dost údajů pro pilotní automatický odhad.")
    a,b=st.columns(2)
    a.metric("Orientační cena bez DPH",f"{lo:,.0f}–{hi:,.0f} Kč")
    b.metric("Odhadovaný výrobní čas",f"{total:.0f} min")
    st.caption("Pilotní odhad podle současného modelu. Finální technologii, termín a cenu potvrzuje technolog Tvarsteel. Model nyní kalibrujeme na skutečných výrobních časech.")
else:
    missing=[]
    if path<=0: missing.append("CAD řezná dráha")
    if height<=0: missing.append("výška řezu")
    if material=="Nezjištěno / vyberte": missing.append("materiál")
    st.info("Pro automatický cenový odhad doplňte: "+", ".join(missing)+".")

st.markdown("### 4. Odeslat poptávku")
company=st.text_input("Firma")
name=st.text_input("Jméno")
email=st.text_input("E-mail")
phone=st.text_input("Telefon (volitelné)")
note=st.text_area("Poznámka / tolerance / požadovaný termín")
if st.button("Odeslat poptávku technologovi",type="primary",use_container_width=True):
    if "@" not in email:
        st.error("Doplňte platný e-mail.")
    else:
        calc_price=price if ready else 0.0
        calc_cut=cut if ready else 0.0
        c=db(); c.execute("""INSERT INTO quotes(company,name,email,material,height,pieces,path,cutting,price,note) VALUES(?,?,?,?,?,?,?,?,?,?)""",(company,name,email,material,height,pieces,path,calc_cut,calc_price,(note+f" | Telefon: {phone}").strip()))
        c.commit(); c.close()
        st.success("Poptávka byla uložena k potvrzení technologem.")

with st.expander("Interní poznámka k pilotu"):
    st.write("Tato Customer Quote verze používá stejný výpočetní základ jako v0.12.1. Před veřejným ostrým nasazením je nutné dokončit kalibraci času a doplnit bezpečné serverové odesílání/ukládání zákaznických souborů.")
