from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
import json
import shutil
import zipfile
import sqlite3
import hashlib
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from pathlib import Path

from sqlite_brain_builder.gui.system_metrics import ProcessMetricSampler

_CLASSIFIED_FALLBACK_FILETYPES = {
    "github": [("Repository packages", "*.zip")],
    "local_code": [("Code packages", "*.zip")],
    "chat_lineage": [("Chat exports", "*.docx *.json *.jsonl *.md *.txt *.zip")],
    "discussion": [("Discussion sources", "*.docx *.md *.pdf *.txt")],
    "analysis": [("Analysis sources", "*.docx *.md *.pdf *.txt")],
    "plan": [("Plan sources", "*.docx *.json *.md *.pdf *.txt")],
    "mode": [("Mode sources", "*.docx *.json *.md *.txt")],
    "docs": [("Document sources", "*.doc *.docx *.html *.md *.odt *.rst *.rtf *.txt *.xml")],
    "data_excel_csv": [("Data sources", "*.csv *.json *.jsonl *.parquet *.tsv *.xls *.xlsm *.xlsx")],
    "ppt_presentation": [("Presentation sources", "*.odp *.ppt *.pptx")],
    "pdf_ocr": [("PDF sources", "*.pdf")],
    "images_ocr": [("Image sources", "*.bmp *.jpeg *.jpg *.png *.tif *.tiff *.webp")],
    "artifacts": [("Artifact sources", "*.csv *.glb *.gltf *.html *.ipynb *.json *.jsonl *.md *.mmd *.mp4 *.parquet *.svg *.txt *.xml")],
    "custom": [("Governed custom sources", "*.csv *.db *.docx *.html *.json *.jsonl *.md *.pdf *.sqlite *.sqlite3 *.tsv *.txt *.xml *.yaml *.yml *.zip")],
}

try:
    from sqlite_brain_builder.runtime.universal_lane_registry import (
        TAB_ORDER, SIDEBAR_LANES, lane_labels, lane_key_from_label, get_lane, key_from_tab
    )
except Exception:
    TAB_ORDER = ["GitHub", "Local Code", "Chat Lineage", "Discussion", "Analysis", "Plan", "Mode", "Docs", "Data", "PPT", "PDF/OCR", "Images/OCR", "Artifacts", "Custom", "Packages", "Receipts"]
    SIDEBAR_LANES = [(x, x) for x in TAB_ORDER if x not in {"Packages", "Receipts"}]
    def lane_labels(): return TAB_ORDER
    def lane_key_from_label(label): return str(label).lower().replace(" ", "_").replace("/", "_")
    def key_from_tab(tab): return lane_key_from_label(tab)
    def get_lane(key):
        normalized = str(key).casefold()
        return {
            "label": str(key).replace("_", " ").title(),
            "tab": str(key).replace("_", " ").title(),
            "filetypes": _CLASSIFIED_FALLBACK_FILETYPES.get(normalized, _CLASSIFIED_FALLBACK_FILETYPES["custom"]),
        }

try:
    from sqlite_brain_builder.runtime.app_backend_v48 import build_fast_brain
except Exception:
    try:
        from sqlite_brain_builder.runtime.whale_runtime_v51 import build_brain as build_fast_brain
    except Exception:
        build_fast_brain = None

build_brain = build_fast_brain

try:
    from sqlite_brain_builder.runtime.package_render_v48 import render_topology_only
except Exception:
    try:
        from sqlite_brain_builder.runtime.whale_runtime_v51 import render_topology as render_topology_only
    except Exception:
        render_topology_only = None

from sqlite_brain_builder.runtime.path_policy import normalize_workspace_dir, brain_output_dir

try:
    from sqlite_brain_builder.runtime.stable_runtime_v53 import LANE_DEFS as _STABLE_LANE_DEFS
    LANE_DEFS = _STABLE_LANE_DEFS
except Exception:
    LANE_DEFS = {
        "docs": {"label": "Docs", "tab": "Docs", "filetypes": _CLASSIFIED_FALLBACK_FILETYPES["docs"]},
        "data_excel_csv": {"label": "Data / Excel / CSV", "tab": "Data", "filetypes": _CLASSIFIED_FALLBACK_FILETYPES["data_excel_csv"]},
        "ppt_presentation": {"label": "PPT / Presentation", "tab": "PPT", "filetypes": _CLASSIFIED_FALLBACK_FILETYPES["ppt_presentation"]},
        "pdf_ocr": {"label": "PDF / OCR", "tab": "PDF/OCR", "filetypes": _CLASSIFIED_FALLBACK_FILETYPES["pdf_ocr"]},
        "images_ocr": {"label": "Images / OCR", "tab": "Images/OCR", "filetypes": _CLASSIFIED_FALLBACK_FILETYPES["images_ocr"]},
        "artifacts": {"label": "Artifacts", "tab": "Artifacts", "filetypes": _CLASSIFIED_FALLBACK_FILETYPES["artifacts"]},
        "chat_lineage": {"label": "Chat Lineage", "tab": "Chat Lineage", "filetypes": _CLASSIFIED_FALLBACK_FILETYPES["chat_lineage"]},
        "local_code": {"label": "Local Code", "tab": "Local Code", "filetypes": _CLASSIFIED_FALLBACK_FILETYPES["local_code"]},
        "github": {"label": "GitHub", "tab": "GitHub", "filetypes": _CLASSIFIED_FALLBACK_FILETYPES["github"]},
        "custom": {"label": "Custom", "tab": "Custom", "filetypes": _CLASSIFIED_FALLBACK_FILETYPES["custom"]},
    }


LANE_KEYS = [
    "github", "local_code", "chat_lineage", "discussion", "analysis", "plan", "mode",
    "docs", "data_excel_csv", "ppt_presentation", "pdf_ocr", "images_ocr", "artifacts", "custom",
]

LANE_FALLBACKS = {
    "github": {"label": "GitHub", "tab": "GitHub", "filetypes": _CLASSIFIED_FALLBACK_FILETYPES["github"]},
    "local_code": {"label": "Local Code", "tab": "Local Code", "filetypes": _CLASSIFIED_FALLBACK_FILETYPES["local_code"]},
    "chat_lineage": {"label": "Chat Lineage", "tab": "Chat Lineage", "filetypes": _CLASSIFIED_FALLBACK_FILETYPES["chat_lineage"]},
    "discussion": {"label": "Discussion", "tab": "Discussion", "filetypes": _CLASSIFIED_FALLBACK_FILETYPES["discussion"]},
    "analysis": {"label": "Analysis", "tab": "Analysis", "filetypes": _CLASSIFIED_FALLBACK_FILETYPES["analysis"]},
    "plan": {"label": "Plan", "tab": "Plan", "filetypes": _CLASSIFIED_FALLBACK_FILETYPES["plan"]},
    "mode": {"label": "Mode", "tab": "Mode", "filetypes": _CLASSIFIED_FALLBACK_FILETYPES["mode"]},
    "docs": {"label": "Docs", "tab": "Docs", "filetypes": _CLASSIFIED_FALLBACK_FILETYPES["docs"]},
    "data_excel_csv": {"label": "Data / Excel / CSV", "tab": "Data", "filetypes": _CLASSIFIED_FALLBACK_FILETYPES["data_excel_csv"]},
    "ppt_presentation": {"label": "PPT / Presentation", "tab": "PPT", "filetypes": _CLASSIFIED_FALLBACK_FILETYPES["ppt_presentation"]},
    "pdf_ocr": {"label": "PDF / OCR", "tab": "PDF/OCR", "filetypes": _CLASSIFIED_FALLBACK_FILETYPES["pdf_ocr"]},
    "images_ocr": {"label": "Images / OCR", "tab": "Images/OCR", "filetypes": _CLASSIFIED_FALLBACK_FILETYPES["images_ocr"]},
    "artifacts": {"label": "Artifacts", "tab": "Artifacts", "filetypes": _CLASSIFIED_FALLBACK_FILETYPES["artifacts"]},
    "custom": {"label": "Custom", "tab": "Custom", "filetypes": _CLASSIFIED_FALLBACK_FILETYPES["custom"]},
}

def _lane_def(key: str) -> dict:
    base = dict(LANE_FALLBACKS.get(key, LANE_FALLBACKS["custom"]))
    try:
        loaded = get_lane(key)
        if isinstance(loaded, dict):
            merged = dict(base)
            merged.update({k: v for k, v in loaded.items() if v is not None})
            if "filetypes" not in merged or not merged["filetypes"]:
                merged["filetypes"] = base["filetypes"]
            return merged
    except Exception:
        pass
    return base

LANE_DEFS = {key: _lane_def(key) for key in LANE_KEYS}



# === V5.9 FUNCTIONAL RUNTIME WIRING START ===
from sqlite_brain_builder.runtime.stable_runtime_v53 import (
    LANE_DEFS,
    scan_tools as runtime_scan_tools,
    install_missing_dependencies as runtime_install_deps,
    build_brain as runtime_build_brain,
    render_topology,
    export_one_upload_package,
    export_gemini_exact10,
    run_hidden,
    LOCKED_FLASH_PROMPT,
)
# === V5.9 FUNCTIONAL RUNTIME WIRING END ===

UI = {
    "bg": "#edf4fb",
    "panel": "#f7fbff",
    "panel_2": "#e7f1f9",
    "sidebar": "#e8f1f8",
    "sidebar_2": "#f8fcff",
    "surface": "#ffffff",
    "line": "#b7cad8",
    "line_soft": "#d7e6f0",
    "shadow": "#cbdce8",
    "glass": "#f9fcff",
    "glass_alt": "#eef7fc",
    "text": "#11283a",
    "muted": "#5c7283",
    "accent": "#1d78a8",
    "accent_soft": "#d8f2ff",
    "accent_2": "#b68b32",
    "ok": "#13875b",
    "warn": "#a87520",
    "danger": "#b83a4b",
    "button": "#edf6fc",
    "button_hover": "#d9eef8",
    "button_gold": "#fff4d8",
    "button_gold_hover": "#f8e2ae",
    "button_active": "#dff4ff",
    "button_active_hover": "#cbebfb",
    "button_danger": "#ffe8ec",
    "button_danger_hover": "#ffd6df",
    "brain_card": "#fbfdff",
    "brain_card_active": "#e0f4ff",
}


def _round_rect(canvas, x1, y1, x2, y2, radius=14, **kwargs):
    points = [
        x1 + radius, y1,
        x2 - radius, y1,
        x2, y1,
        x2, y1 + radius,
        x2, y2 - radius,
        x2, y2,
        x2 - radius, y2,
        x1 + radius, y2,
        x1, y2,
        x1, y2 - radius,
        x1, y1 + radius,
        x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


class PillButton(tk.Canvas):
    def __init__(self, parent, text, command=None, width=150, height=34, fill=None, hover=None, fg=None, outline=None):
        super().__init__(
            parent,
            width=width,
            height=height,
            bg=parent.cget("bg"),
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.text = text
        self.command = command
        self.fill = fill or UI["button"]
        self.hover = hover or UI["button_hover"]
        self.fg = fg or UI["text"]
        self.outline = outline or UI["line"]
        self.current_fill = self.fill
        self.bind("<Configure>", self._draw)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", self._click)

    def _draw(self, event=None):
        self.delete("all")
        w = max(20, self.winfo_width())
        h = max(20, self.winfo_height())
        radius = min(16, h // 2)
        _round_rect(self, 3, 5, w - 2, h - 1, radius, fill=UI["shadow"], outline=UI["shadow"], width=0)
        _round_rect(self, 2, 2, w - 3, h - 4, radius, fill=self.current_fill, outline=self.outline, width=1)
        self.create_line(12, 6, w - 14, 6, fill="#ffffff", width=1)
        self.create_text(
            w // 2,
            h // 2 - 1,
            text=self.text,
            fill=self.fg,
            font=("Segoe UI", 9, "bold"),
            width=w - 18,
        )

    def _enter(self, event=None):
        self.current_fill = self.hover
        self._draw()

    def _leave(self, event=None):
        self.current_fill = self.fill
        self._draw()

    def _click(self, event=None):
        if callable(self.command):
            self.command()

    def set_text(self, text):
        self.text = text
        self._draw()


class EvidenceLogo(tk.Canvas):
    def __init__(self, parent, size=46, bg=None):
        super().__init__(
            parent,
            width=size,
            height=size,
            bg=bg or parent.cget("bg"),
            highlightthickness=0,
            bd=0,
        )
        self.size = size
        self.bind("<Configure>", self._draw)

    def _draw(self, event=None):
        self.delete("all")
        w = max(24, self.winfo_width())
        h = max(24, self.winfo_height())
        cx = w / 2
        cy = h / 2
        r = min(w, h) * 0.34
        self.create_oval(cx - r, cy - r, cx + r, cy + r, fill="#ffffff", outline=UI["line"], width=1)
        self.create_arc(cx - r * 1.2, cy - r * 1.2, cx + r * 1.2, cy + r * 1.2, start=18, extent=120, outline="#64c7ec", width=3, style="arc")
        self.create_arc(cx - r * 1.2, cy - r * 1.2, cx + r * 1.2, cy + r * 1.2, start=150, extent=95, outline="#e7c76a", width=3, style="arc")
        self.create_arc(cx - r * 1.2, cy - r * 1.2, cx + r * 1.2, cy + r * 1.2, start=258, extent=90, outline="#91c9ff", width=3, style="arc")
        self.create_line(cx, cy - r * 0.62, cx, cy + r * 0.62, fill=UI["accent"], width=2)
        self.create_line(cx - r * 0.55, cy, cx + r * 0.55, cy, fill=UI["accent_2"], width=2)
        self.create_oval(cx - 3, cy - 3, cx + 3, cy + 3, fill=UI["accent"], outline="")


class FlowBackground(tk.Canvas):
    def __init__(self, parent, height=74):
        super().__init__(parent, height=height, bg=parent.cget("bg"), highlightthickness=0, bd=0)
        self.phase = 0
        self.bind("<Configure>", self._draw)
        self.after(80, self._animate)

    def _animate(self):
        self.phase = (self.phase + 1) % 120
        self._draw()
        self.after(80, self._animate)

    def _draw(self, event=None):
        self.delete("all")
        w = max(320, self.winfo_width())
        h = max(52, self.winfo_height())
        y = h * 0.54
        colors = ["#9bdff6", "#f0d486", "#bfe9ff"]
        for i in range(7):
            x = (i * w / 6 + self.phase * 2) % (w + 80) - 40
            nxt = min(w - 12, x + 120)
            self.create_line(x, y + ((i % 3) - 1) * 12, nxt, y + (((i + 1) % 3) - 1) * 12, fill=colors[i % 3], width=2, smooth=True)
            self.create_oval(x - 4, y - 4 + ((i % 3) - 1) * 12, x + 4, y + 4 + ((i % 3) - 1) * 12, fill=colors[i % 3], outline="")
        self.create_text(18, 18, anchor="w", text="SQLite Builder route flow", fill=UI["muted"], font=("Segoe UI", 9, "bold"))


class BrainPulse(tk.Canvas):
    def __init__(self, parent, size=42, bg=None):
        super().__init__(parent, width=size, height=size, bg=bg or parent.cget("bg"), highlightthickness=0, bd=0)
        self.size = size
        self.running = False
        self.phase = 0
        self.bind("<Configure>", self._draw)

    def start(self):
        if not self.running:
            self.running = True
            self._animate()

    def stop(self):
        self.running = False
        self.phase = 0
        self._draw()

    def _animate(self):
        if not self.running:
            return
        self.phase = (self.phase + 1) % 28
        self._draw()
        self.after(70, self._animate)

    def _draw(self, event=None):
        self.delete("all")
        w = max(28, self.winfo_width())
        h = max(28, self.winfo_height())
        cx = w / 2
        cy = h / 2
        base = min(w, h) * 0.27
        pulse = (self.phase % 14) / 14 if self.running else 0
        radius = base + pulse * 8
        outline = "#85dfff" if self.running else UI["line"]
        self.create_oval(cx - radius, cy - radius, cx + radius, cy + radius, outline=outline, width=2)
        self.create_oval(cx - base, cy - base, cx + base, cy + base, fill="#ffffff", outline=UI["accent"], width=1)
        self.create_arc(cx - base * 0.95, cy - base * 0.8, cx + base * 0.25, cy + base * 0.8, start=90, extent=190, outline=UI["accent"], width=2, style="arc")
        self.create_arc(cx - base * 0.25, cy - base * 0.8, cx + base * 0.95, cy + base * 0.8, start=260, extent=190, outline=UI["accent_2"], width=2, style="arc")
        self.create_line(cx, cy - base * 0.75, cx, cy + base * 0.75, fill=UI["line"], width=1)
        self.create_oval(cx - 2, cy - 2, cx + 2, cy + 2, fill=UI["accent"], outline="")


class MainWindow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Evidence OS - SQLite Builder")
        self.geometry("1560x900+20+20")
        self.minsize(1180, 720)
        self.configure(bg=UI["bg"])
        self.workspace_dir=tk.StringVar(value=str(Path.home()/"Downloads"/"_0000"))
        self.brain_name=tk.StringVar(value="New Brain")
        self.active_module=tk.StringVar(value="SQLite Builder")
        self.active_chat_var=tk.StringVar(value="SQLite Builder session")
        self.output_dir_var=tk.StringVar(value="")
        self.request_var=tk.StringVar(value="")
        self.sources=[]
        self.pinned_brains=set()
        self.metric_sampler=ProcessMetricSampler()
        self.metric_vars={}
        self.tabs={}; self.trees={}; self.q=queue.Queue(); self.started_at=None
        self._ui(); self.refresh_brain_list(); self._sync_session_labels(); self.after(100,self._poll); self.after(1000,self._tick); self.after(1200,self._refresh_metrics)

    def _ui(self):
        self._ui_evidence_os()
        return
        self._style()
        shell=tk.Frame(self,bg=UI["bg"])
        shell.pack(fill="both",expand=True)

        left=tk.Frame(shell,bg=UI["sidebar"],width=330)
        left.pack(side="left",fill="y")
        left.pack_propagate(False)
        self.sidebar = left
        self.sidebar_collapsed = False
        self.brain_names = []

        right=tk.Frame(shell,bg=UI["bg"])
        right.pack(side="left",fill="both",expand=True)

        brand=tk.Frame(left,bg=UI["sidebar"])
        brand.pack(fill="x",padx=18,pady=(18,10))
        tk.Label(brand,text="Evidence OS",font=("Segoe UI",17,"bold"),bg=UI["sidebar"],fg=UI["text"]).pack(anchor="w")
        tk.Label(brand,text="SQLite Builder",font=("Segoe UI",11),bg=UI["sidebar"],fg=UI["accent"]).pack(anchor="w")
        tk.Label(brand,text="one project topology / no model dependency",font=("Segoe UI",9),bg=UI["sidebar"],fg=UI["muted"]).pack(anchor="w",pady=(4,0))

        brain_bar=tk.Frame(left,bg=UI["sidebar"])
        brain_bar.pack(fill="x",padx=16,pady=(4,8))
        self.sidebar_toggle_pill=self._pill(brain_bar,"Hide",self.toggle_sidebar,width=62)
        self.sidebar_toggle_pill.pack(side="left",padx=(0,6))
        self._pill(brain_bar,"New",self.new_brain,width=62).pack(side="left",padx=(0,6))
        self._pill(brain_bar,"Refresh",self.refresh_brain_list,width=82).pack(side="left",padx=(0,6))
        self._pill(brain_bar,"Folder",self.open_output_folder,width=70,kind="gold").pack(side="left")

        tk.Label(left,text="Brain chats",font=("Segoe UI",10,"bold"),bg=UI["sidebar"],fg=UI["muted"]).pack(anchor="w",padx=18,pady=(8,4))
        brain_box=tk.Frame(left,bg=UI["sidebar"],highlightthickness=1,highlightbackground=UI["line_soft"])
        brain_box.pack(fill="x",padx=16,pady=2)
        self.brain_canvas=tk.Canvas(brain_box,bg=UI["sidebar_2"],highlightthickness=0,height=214)
        self.brain_scroll=ttk.Scrollbar(brain_box,orient="vertical",command=self.brain_canvas.yview)
        self.brain_rows=tk.Frame(self.brain_canvas,bg=UI["sidebar_2"])
        self.brain_canvas_window=self.brain_canvas.create_window((0,0),window=self.brain_rows,anchor="nw")
        self.brain_canvas.configure(yscrollcommand=self.brain_scroll.set)
        self.brain_rows.bind("<Configure>",lambda e: self.brain_canvas.configure(scrollregion=self.brain_canvas.bbox("all")))
        self.brain_canvas.bind("<Configure>",lambda e: self.brain_canvas.itemconfigure(self.brain_canvas_window,width=e.width))
        self.brain_canvas.pack(side="left",fill="both",expand=True)
        self.brain_scroll.pack(side="right",fill="y")
        self._bind_wheel(self.brain_canvas)

        self.brain_detail_var=tk.StringVar(value="Select a brain to inspect its source queue.")
        tk.Label(left,textvariable=self.brain_detail_var,wraplength=276,justify="left",font=("Segoe UI",9),bg=UI["sidebar"],fg=UI["muted"]).pack(fill="x",padx=18,pady=(8,12))

        tk.Label(left,text="Lane pills",font=("Segoe UI",10,"bold"),bg=UI["sidebar"],fg=UI["muted"]).pack(anchor="w",padx=18,pady=(2,5))
        lane_canvas=tk.Canvas(left,bg=UI["sidebar"],highlightthickness=0,height=336)
        lane_scroll=ttk.Scrollbar(left,orient="vertical",command=lane_canvas.yview)
        lane_frame=tk.Frame(lane_canvas,bg=UI["sidebar"])
        lane_window=lane_canvas.create_window((0,0),window=lane_frame,anchor="nw")
        lane_canvas.configure(yscrollcommand=lane_scroll.set)
        lane_frame.bind("<Configure>",lambda e: lane_canvas.configure(scrollregion=lane_canvas.bbox("all")))
        lane_canvas.bind("<Configure>",lambda e: lane_canvas.itemconfigure(lane_window,width=e.width))
        lane_canvas.pack(side="left",fill="both",expand=True,padx=(16,0),pady=(0,12))
        lane_scroll.pack(side="right",fill="y",pady=(0,12))
        self._bind_wheel(lane_canvas)
        for tab in ["GitHub","Local Code","Chat Lineage","Discussion","Analysis","Plan","Mode","Docs","Data","PPT","PDF/OCR","Images/OCR","Artifacts","Custom","Packages","Receipts"]:
            self._pill(lane_frame,tab,lambda t=tab:self.select_tab(t),width=260,height=30).pack(fill="x",pady=3)

        header=tk.Frame(right,bg=UI["panel"],highlightthickness=1,highlightbackground=UI["line"])
        header.pack(fill="x",padx=16,pady=(16,8))
        title_col=tk.Frame(header,bg=UI["panel"])
        title_col.pack(side="left",fill="x",expand=True,padx=18,pady=14)
        tk.Label(title_col,text="SQLite Builder Chat Console",font=("Segoe UI",18,"bold"),bg=UI["panel"],fg=UI["text"]).pack(anchor="w")
        tk.Label(title_col,text="Build a SQLite brain, inspect one topology, export clean packages.",font=("Segoe UI",10),bg=UI["panel"],fg=UI["muted"]).pack(anchor="w",pady=(4,0))
        chips=tk.Frame(header,bg=UI["panel"])
        chips.pack(side="right",padx=16,pady=14)
        for text,kind in [("SQLite",None),("One MMD",None),("Env/UOP locked","gold"),("Code sector",None)]:
            self._pill(chips,text,lambda:None,width=114,height=30,kind=kind).pack(side="left",padx=4)

        target=tk.Frame(right,bg=UI["bg"])
        target.pack(fill="x",padx=16,pady=(0,8))
        tk.Label(target,text="Workspace",font=("Segoe UI",9,"bold"),bg=UI["bg"],fg=UI["muted"]).grid(row=0,column=0,sticky="w",padx=(0,8))
        self.workspace_entry=tk.Entry(target,textvariable=self.workspace_dir,bg=UI["panel_2"],fg=UI["text"],insertbackground=UI["text"],relief="flat",bd=0,font=("Segoe UI",10))
        self.workspace_entry.grid(row=0,column=1,sticky="ew",ipady=8)
        self._pill(target,"Choose",self.choose_workspace,width=92,height=34).grid(row=0,column=2,padx=8)
        tk.Label(target,text="Brain",font=("Segoe UI",9,"bold"),bg=UI["bg"],fg=UI["muted"]).grid(row=1,column=0,sticky="w",padx=(0,8),pady=(8,0))
        self.brain_entry=tk.Entry(target,textvariable=self.brain_name,bg=UI["panel_2"],fg=UI["text"],insertbackground=UI["text"],relief="flat",bd=0,font=("Segoe UI",11,"bold"))
        self.brain_entry.grid(row=1,column=1,sticky="ew",ipady=8,pady=(8,0))
        self._pill(target,"Use Brain",self.new_brain,width=92,height=34,kind="gold").grid(row=1,column=2,padx=8,pady=(8,0))
        target.columnconfigure(1,weight=1)

        command_deck=tk.Frame(right,bg=UI["glass"],highlightthickness=1,highlightbackground=UI["line_soft"])
        command_deck.pack(fill="x",padx=16,pady=(0,8))
        action_row=tk.Frame(command_deck,bg=UI["glass"])
        action_row.pack(fill="x",padx=12,pady=(10,4))
        for label,cmd,kind,width in [
            ("Build Brain",self.build_brain,"gold",126),
            ("Render Topology",self.render,None,138),
            ("ChatGPT Export",self.export,None,132),
            ("Gemini Export",self.gemini,None,132),
            ("Preview",self.preview,None,92),
            ("Unload",self.unload_selected,None,92),
            ("Output Folder",self.open_output_folder,None,122),
            ("Flash",self.view_flash_prompt,"gold",80),
        ]:
            self._pill(action_row,label,cmd,width=width,height=34,kind=kind).pack(side="left",padx=(0,8),pady=2)
        intake_top=tk.Frame(command_deck,bg=UI["glass"])
        intake_top.pack(fill="x",padx=12,pady=(0,10))
        for label,cmd in [
            ("GitHub", self.add_github), ("Local Code", self.add_local_code),
            ("Chat", lambda:self.load_lane_file("chat_lineage")),
            ("Docs", lambda:self.load_lane_file("docs")),
            ("Data", lambda:self.load_lane_file("data_excel_csv")),
            ("PDF", lambda:self.load_lane_file("pdf_ocr")),
            ("Images", lambda:self.load_lane_file("images_ocr")),
            ("Artifacts", lambda:self.load_lane_file("artifacts")),
            ("Custom", self.add_custom_lane),
            ("Tools", self.scan_tools),
        ]:
            self._pill(intake_top,label,cmd,width=92,height=30).pack(side="left",padx=(0,7),pady=2)

        body=ttk.PanedWindow(right,orient="vertical")
        body.pack(fill="both",expand=True,padx=16,pady=(0,8))

        chat_panel=tk.Frame(body,bg=UI["panel"],highlightthickness=1,highlightbackground=UI["line"])
        body.add(chat_panel,weight=4)
        chat_head=tk.Frame(chat_panel,bg=UI["panel"])
        chat_head.pack(fill="x",padx=14,pady=(12,6))
        tk.Label(chat_head,text="Brain conversation",font=("Segoe UI",11,"bold"),bg=UI["panel"],fg=UI["text"]).pack(side="left")
        self._pill(chat_head,"Topology View",self.render,width=118,height=30,kind="gold").pack(side="right",padx=(6,0))
        self._pill(chat_head,"Flash",self.view_flash_prompt,width=76,height=30).pack(side="right",padx=(6,0))
        chat_body=tk.Frame(chat_panel,bg=UI["panel"])
        chat_body.pack(fill="both",expand=True,padx=14,pady=(0,12))
        chat_scroll=ttk.Scrollbar(chat_body,orient="vertical")
        self.chat=tk.Text(
            chat_body,
            wrap="word",
            bg=UI["surface"],
            fg=UI["text"],
            insertbackground=UI["text"],
            relief="flat",
            bd=0,
            padx=16,
            pady=14,
            font=("Segoe UI",10),
            yscrollcommand=chat_scroll.set,
        )
        chat_scroll.configure(command=self.chat.yview)
        self.chat.pack(side="left",fill="both",expand=True)
        chat_scroll.pack(side="right",fill="y")
        self.chat.tag_config("system",foreground=UI["accent"],spacing1=8,spacing3=8,font=("Segoe UI",10,"bold"))
        self.chat.tag_config("assistant",foreground=UI["text"],spacing1=4,spacing3=8)
        self.chat.tag_config("user",foreground=UI["accent_2"],spacing1=4,spacing3=8,font=("Segoe UI",10,"bold"))
        self.chat.configure(state="disabled")

        lower=tk.Frame(body,bg=UI["bg"])
        body.add(lower,weight=2)
        self.nb=ttk.Notebook(lower)
        self.nb.pack(fill="both",expand=True)
        for tab in TAB_ORDER: self.ensure_tab(tab)

        intake=tk.Frame(right,bg=UI["panel"],highlightthickness=1,highlightbackground=UI["line"])
        intake.pack(fill="x",padx=16,pady=(0,8))
        tk.Label(intake,text="Source intake pills",font=("Segoe UI",10,"bold"),bg=UI["panel"],fg=UI["muted"]).pack(anchor="w",padx=14,pady=(10,4))
        intake_row=tk.Frame(intake,bg=UI["panel"])
        intake_row.pack(fill="x",padx=12,pady=(0,10))
        for label,cmd in [
            ("Scan Tools", self.scan_tools), ("Install Deps", self.install_deps),
            ("GitHub Repo", self.add_github), ("Local Code", self.add_local_code),
            ("Chat From Start", lambda:self.load_lane_file("chat_lineage")),
            ("Docs", lambda:self.load_lane_file("docs")), ("Data", lambda:self.load_lane_file("data_excel_csv")),
            ("PPT", lambda:self.load_lane_file("ppt_presentation")), ("PDF/OCR", lambda:self.load_lane_file("pdf_ocr")),
            ("Images/OCR", lambda:self.load_lane_file("images_ocr")), ("Artifacts", lambda:self.load_lane_file("artifacts")),
            ("Custom Lane", self.add_custom_lane)]:
            self._pill(intake_row,label,cmd,width=118,height=32).pack(side="left",padx=4,pady=4)

        prog=tk.Frame(right,bg=UI["panel"],highlightthickness=1,highlightbackground=UI["line"])
        prog.pack(fill="x",padx=16,pady=(0,8))
        self.progress=ttk.Progressbar(prog,mode="determinate",maximum=100,style="Brain.Horizontal.TProgressbar")
        self.progress.pack(fill="x",padx=14,pady=(12,6))
        self.process_var=tk.StringVar(value="Current process: idle")
        self.file_var=tk.StringVar(value="Current file: none")
        self.count_var=tk.StringVar(value="Progress: 0/0 (0%)")
        self.elapsed_var=tk.StringVar(value="Elapsed: 0s")
        self.eta_var=tk.StringVar(value="ETA: --")
        self.finish_var=tk.StringVar(value="Estimated finish: --")
        metric_row=tk.Frame(prog,bg=UI["panel"])
        metric_row.pack(fill="x",padx=14,pady=(0,10))
        for v in [self.process_var,self.file_var,self.count_var,self.elapsed_var,self.eta_var,self.finish_var]:
            tk.Label(metric_row,textvariable=v,bg=UI["panel"],fg=UI["muted"],font=("Segoe UI",9),anchor="w").pack(side="left",fill="x",expand=True,padx=(0,10))

        bottom=tk.Frame(right,bg=UI["bg"])
        bottom.pack(fill="x",padx=16,pady=(0,12))
        for label,cmd,kind,width in [
            ("Preview",self.preview,None,98),
            ("Unload",self.unload_selected,None,96),
            ("Build Brain",self.build_brain,"gold",130),
            ("Render Topology",self.render,None,142),
            ("ChatGPT Export",self.export,None,142),
            ("Gemini Export",self.gemini,None,142),
            ("Output Folder",self.open_output_folder,None,130),
            ("View Flash",self.view_flash_prompt,"gold",116),
        ]:
            self._pill(bottom,label,cmd,width=width,height=36,kind=kind).pack(side="left",padx=(0,8),pady=2)
        self.status=tk.StringVar(value="Ready.")
        tk.Label(right,textvariable=self.status,bg=UI["bg"],fg=UI["muted"],anchor="w",font=("Segoe UI",9)).pack(fill="x",padx=18,pady=(0,8))
        self._append_chat("system","Ready","Add a code folder or GitHub repo, build the brain, then render one project topology.")
        self._append_chat("assistant","Topology rule","Only project_master_topology.mmd is generated. It includes locked Env/UOP/project law plus coded project SQLite evidence.")

    def _ui_evidence_os(self):
        self._style()
        shell=tk.Frame(self,bg=UI["bg"])
        shell.pack(fill="both",expand=True)

        self.sidebar=tk.Frame(shell,bg=UI["sidebar"],width=354)
        self.sidebar.pack(side="left",fill="y")
        self.sidebar.pack_propagate(False)
        self.sidebar_collapsed=False
        self.brain_names=[]
        self.sidebar_full=tk.Frame(self.sidebar,bg=UI["sidebar"])
        self.sidebar_full.pack(fill="both",expand=True)
        self.sidebar_rail=tk.Frame(self.sidebar,bg=UI["sidebar"])
        self._build_sidebar_full(self.sidebar_full)
        self._build_sidebar_rail(self.sidebar_rail)

        main=tk.Frame(shell,bg=UI["bg"])
        main.pack(side="left",fill="both",expand=True)
        self._build_evidence_topbar(main)
        self._build_session_strip(main)
        self._build_sqlite_builder_deck(main)
        self._build_work_area(main)
        self._build_task_status(main)

        self.status=tk.StringVar(value="Ready.")
        tk.Label(main,textvariable=self.status,bg=UI["bg"],fg=UI["muted"],anchor="w",font=("Segoe UI",9)).pack(fill="x",padx=18,pady=(0,8))
        self._append_chat("system","Evidence OS ready","SQLite Builder is the active module pill. Add sources, build the brain, then render one project topology.")
        self._append_chat("assistant","Topology rule","Only project_master_topology.mmd is generated. It includes locked Env/UOP/project law plus coded project SQLite evidence.")

    def _glass(self, parent, bg=None, line=None):
        return tk.Frame(parent,bg=bg or UI["glass"],highlightthickness=1,highlightbackground=line or UI["line_soft"])

    def _build_sidebar_full(self, parent):
        brand=tk.Frame(parent,bg=UI["sidebar"])
        brand.pack(fill="x",padx=18,pady=(18,10))
        EvidenceLogo(brand,size=48,bg=UI["sidebar"]).pack(side="left",padx=(0,10))
        brand_text=tk.Frame(brand,bg=UI["sidebar"])
        brand_text.pack(side="left",fill="x",expand=True)
        tk.Label(brand_text,text="Evidence OS",font=("Segoe UI",18,"bold"),bg=UI["sidebar"],fg=UI["text"]).pack(anchor="w")
        tk.Label(brand_text,text="Identity",font=("Segoe UI",10),bg=UI["sidebar"],fg=UI["muted"]).pack(anchor="w")
        self._pill(brand,"Hide",self.toggle_sidebar,width=64,height=32).pack(side="right")

        new_chat=self._glass(parent,bg=UI["glass"])
        new_chat.pack(fill="x",padx=16,pady=(6,10))
        tk.Label(new_chat,text="New Chat",font=("Segoe UI",13,"bold"),bg=UI["glass"],fg=UI["text"]).pack(side="left",padx=14,pady=10)
        self._pill(new_chat,"+",self.new_brain,width=42,height=34,kind="gold").pack(side="right",padx=10,pady=7)

        tk.Label(parent,text="Projects",font=("Segoe UI",12,"bold"),bg=UI["sidebar"],fg=UI["text"]).pack(anchor="w",padx=18,pady=(4,4))
        brain_box=self._glass(parent,bg=UI["sidebar_2"])
        brain_box.pack(fill="x",padx=16,pady=(0,10))
        self.brain_canvas=tk.Canvas(brain_box,bg=UI["sidebar_2"],highlightthickness=0,height=178)
        self.brain_scroll=ttk.Scrollbar(brain_box,orient="vertical",command=self.brain_canvas.yview)
        self.brain_rows=tk.Frame(self.brain_canvas,bg=UI["sidebar_2"])
        self.brain_canvas_window=self.brain_canvas.create_window((0,0),window=self.brain_rows,anchor="nw")
        self.brain_canvas.configure(yscrollcommand=self.brain_scroll.set)
        self.brain_rows.bind("<Configure>",lambda e: self.brain_canvas.configure(scrollregion=self.brain_canvas.bbox("all")))
        self.brain_canvas.bind("<Configure>",lambda e: self.brain_canvas.itemconfigure(self.brain_canvas_window,width=e.width))
        self.brain_canvas.pack(side="left",fill="both",expand=True)
        self.brain_scroll.pack(side="right",fill="y")
        self._bind_wheel(self.brain_canvas)

        active=self._glass(parent,bg=UI["glass"])
        active.pack(fill="x",padx=16,pady=(2,10))
        tk.Label(active,text="Active Chat",font=("Segoe UI",12,"bold"),bg=UI["glass"],fg=UI["text"]).pack(anchor="w",padx=14,pady=(10,2))
        tk.Label(active,textvariable=self.active_chat_var,font=("Segoe UI",10),wraplength=280,justify="left",bg=UI["glass"],fg=UI["muted"]).pack(anchor="w",padx=14,pady=(0,8))
        active_row=tk.Frame(active,bg=UI["glass"])
        active_row.pack(fill="x",padx=10,pady=(0,10))
        self._pill(active_row,"Chats",self._show_project_chats,width=74,height=30).pack(side="left",padx=3)
        self._pill(active_row,"Pin",self._pin_active_brain,width=58,height=30).pack(side="left",padx=3)
        self._pill(active_row,"Unpin",self._unpin_active_brain,width=72,height=30).pack(side="left",padx=3)
        self._pill(active_row,"Remove",self._remove_active_brain,width=78,height=30,kind="danger").pack(side="left",padx=3)

        self.brain_detail_var=tk.StringVar(value="Select a brain to inspect its source queue.")
        tk.Label(parent,textvariable=self.brain_detail_var,wraplength=302,justify="left",font=("Segoe UI",9),bg=UI["sidebar"],fg=UI["muted"]).pack(fill="x",padx=18,pady=(0,10))

        loader=self._glass(parent,bg=UI["glass"])
        loader.pack(fill="x",padx=16,pady=(0,10))
        tk.Label(loader,text="M-Loader Connection",font=("Segoe UI",11,"bold"),bg=UI["glass"],fg=UI["text"]).pack(anchor="w",padx=14,pady=(10,2))
        tk.Label(loader,text="Connected to: SQLite Builder Brain Delta",font=("Segoe UI",9),wraplength=286,justify="left",bg=UI["glass"],fg=UI["muted"]).pack(anchor="w",padx=14,pady=(0,8))
        self._pill(loader,"Add Source",lambda:self._open_lane_popup("local_code"),width=118,height=32,kind="gold").pack(anchor="w",padx=12,pady=(0,10))

        tk.Label(parent,text="Source pills",font=("Segoe UI",10,"bold"),bg=UI["sidebar"],fg=UI["muted"]).pack(anchor="w",padx=18,pady=(0,4))
        lane_canvas=tk.Canvas(parent,bg=UI["sidebar"],highlightthickness=0,height=188)
        lane_scroll=ttk.Scrollbar(parent,orient="vertical",command=lane_canvas.yview)
        lane_frame=tk.Frame(lane_canvas,bg=UI["sidebar"])
        lane_window=lane_canvas.create_window((0,0),window=lane_frame,anchor="nw")
        lane_canvas.configure(yscrollcommand=lane_scroll.set)
        lane_frame.bind("<Configure>",lambda e: lane_canvas.configure(scrollregion=lane_canvas.bbox("all")))
        lane_canvas.bind("<Configure>",lambda e: lane_canvas.itemconfigure(lane_window,width=e.width))
        lane_canvas.pack(side="left",fill="both",expand=True,padx=(16,0),pady=(0,12))
        lane_scroll.pack(side="right",fill="y",pady=(0,12))
        self._bind_wheel(lane_canvas)
        for label,key in self._source_pill_specs():
            self._pill(lane_frame,label,lambda k=key:self._open_lane_popup(k),width=282,height=30).pack(fill="x",pady=3)

    def _build_sidebar_rail(self, parent):
        EvidenceLogo(parent,size=50,bg=UI["sidebar"]).pack(pady=(18,20))
        for label,cmd,kind in [
            ("Open",self.toggle_sidebar,None),
            ("New",self.new_brain,"gold"),
            ("Search",self.refresh_brain_list,None),
            ("Pin",self._pin_active_brain,None),
            ("M-Loader",lambda:self._open_lane_popup("local_code"),None),
        ]:
            self._pill(parent,label,cmd,width=86,height=34,kind=kind).pack(padx=10,pady=8)

    def _build_evidence_topbar(self, parent):
        top=self._glass(parent,bg=UI["panel"],line=UI["line"])
        top.pack(fill="x",padx=16,pady=(16,8))
        left=tk.Frame(top,bg=UI["panel"])
        left.pack(side="left",fill="x",expand=True,padx=16,pady=12)
        tk.Label(left,text="Evidence OS",font=("Segoe UI",22,"bold"),bg=UI["panel"],fg=UI["text"]).pack(anchor="w")
        tk.Label(left,text="One workspace shell. SQLite Builder is the active module pill.",font=("Segoe UI",10),bg=UI["panel"],fg=UI["muted"]).pack(anchor="w",pady=(2,0))
        FlowBackground(left,height=56).pack(fill="x",pady=(7,0))
        modules=tk.Frame(top,bg=UI["panel"])
        modules.pack(side="right",padx=12,pady=12)
        self._pill(modules,"SQLite Builder",lambda:None,width=132,height=34,kind="active").pack(side="left",padx=4)
        for label in ["AI Powerhouse","M-Loader","Admin Cockpit"]:
            self._pill(modules,label,lambda n=label:self._module_placeholder(n),width=128,height=34).pack(side="left",padx=4)
        self._pill(modules,"Esc Admin",self._open_admin_placeholder,width=106,height=34,kind="gold").pack(side="left",padx=(10,4))

    def _build_session_strip(self, parent):
        strip=self._glass(parent,bg=UI["glass"])
        strip.pack(fill="x",padx=16,pady=(0,8))
        tk.Label(strip,text="Workspace",font=("Segoe UI",9,"bold"),bg=UI["glass"],fg=UI["muted"]).grid(row=0,column=0,sticky="w",padx=(14,8),pady=(10,4))
        self.workspace_entry=tk.Entry(strip,textvariable=self.workspace_dir,bg=UI["panel_2"],fg=UI["text"],insertbackground=UI["text"],relief="flat",bd=0,font=("Segoe UI",10))
        self.workspace_entry.grid(row=0,column=1,sticky="ew",ipady=8,pady=(10,4))
        self._pill(strip,"Choose",self.choose_workspace,width=90,height=32).grid(row=0,column=2,padx=8,pady=(10,4))
        tk.Label(strip,text="Brain",font=("Segoe UI",9,"bold"),bg=UI["glass"],fg=UI["muted"]).grid(row=1,column=0,sticky="w",padx=(14,8),pady=(4,10))
        self.brain_entry=tk.Entry(strip,textvariable=self.brain_name,bg=UI["panel_2"],fg=UI["text"],insertbackground=UI["text"],relief="flat",bd=0,font=("Segoe UI",11,"bold"))
        self.brain_entry.grid(row=1,column=1,sticky="ew",ipady=8,pady=(4,10))
        self._pill(strip,"Use Brain",self.new_brain,width=100,height=32,kind="gold").grid(row=1,column=2,padx=8,pady=(4,10))
        tk.Label(strip,textvariable=self.output_dir_var,font=("Segoe UI",9),bg=UI["glass"],fg=UI["accent"]).grid(row=2,column=1,sticky="w",pady=(0,10))
        strip.columnconfigure(1,weight=1)

    def _build_sqlite_builder_deck(self, parent):
        deck=self._glass(parent,bg=UI["glass"])
        deck.pack(fill="x",padx=16,pady=(0,8))
        head=tk.Frame(deck,bg=UI["glass"])
        head.pack(fill="x",padx=14,pady=(10,4))
        self.brain_pulse=BrainPulse(head,size=42,bg=UI["glass"])
        self.brain_pulse.pack(side="left",padx=(0,10))
        tk.Label(head,text="SQLite Builder",font=("Segoe UI",16,"bold"),bg=UI["glass"],fg=UI["text"]).pack(side="left")
        for text,kind,width in [("One MMD","active",92),("Env/UOP locked","gold",126),("No model dependency",None,154),("Code sector",None,108)]:
            self._pill(head,text,lambda:None,width=width,height=30,kind=kind).pack(side="right",padx=4)
        actions=tk.Frame(deck,bg=UI["glass"])
        actions.pack(fill="x",padx=12,pady=(4,6))
        for label,cmd,kind,width in [
            ("Build Brain",self.build_brain,"gold",126),
            ("Render Topology",self.render,None,138),
            ("ChatGPT Export",self.export,None,132),
            ("Gemini Export",self.gemini,None,132),
            ("Preview",self.preview,None,92),
            ("Unload",self.unload_selected,None,92),
            ("Output Folder",self.open_output_folder,None,126),
            ("Scan Tools",self.scan_tools,None,112),
            ("View Flash",self.view_flash_prompt,"gold",112),
        ]:
            self._pill(actions,label,cmd,width=width,height=34,kind=kind).pack(side="left",padx=(0,8),pady=2)
        source=tk.Frame(deck,bg=UI["glass"])
        source.pack(fill="x",padx=12,pady=(0,10))
        for label,key in self._source_pill_specs(compact=True):
            self._pill(source,label,lambda k=key:self._open_lane_popup(k),width=104,height=30).pack(side="left",padx=(0,7),pady=2)

    def _build_work_area(self, parent):
        body=ttk.PanedWindow(parent,orient="vertical")
        body.pack(fill="both",expand=True,padx=16,pady=(0,8))

        chat_panel=self._glass(body,bg=UI["panel"],line=UI["line"])
        body.add(chat_panel,weight=4)
        chat_head=tk.Frame(chat_panel,bg=UI["panel"])
        chat_head.pack(fill="x",padx=14,pady=(12,6))
        tk.Label(chat_head,text="Brain conversation",font=("Segoe UI",12,"bold"),bg=UI["panel"],fg=UI["text"]).pack(side="left")
        self._pill(chat_head,"Topology View",self.render,width=122,height=30,kind="gold").pack(side="right",padx=(6,0))
        self._pill(chat_head,"Flash",self.view_flash_prompt,width=78,height=30).pack(side="right",padx=(6,0))
        route=tk.Frame(chat_head,bg=UI["panel"])
        route.pack(side="right",padx=(6,14))
        self._pill(route,"Local AI off",lambda:None,width=100,height=30).pack(side="left",padx=3)
        self._pill(route,"Cloud AI off",lambda:None,width=104,height=30).pack(side="left",padx=3)

        chat_body=tk.Frame(chat_panel,bg=UI["panel"])
        chat_body.pack(fill="both",expand=True,padx=14,pady=(0,8))
        chat_scroll=ttk.Scrollbar(chat_body,orient="vertical")
        self.chat=tk.Text(
            chat_body,
            wrap="word",
            bg=UI["surface"],
            fg=UI["text"],
            insertbackground=UI["text"],
            relief="flat",
            bd=0,
            padx=18,
            pady=16,
            font=("Segoe UI",10),
            yscrollcommand=chat_scroll.set,
        )
        chat_scroll.configure(command=self.chat.yview)
        self.chat.pack(side="left",fill="both",expand=True)
        chat_scroll.pack(side="right",fill="y")
        self.chat.tag_config("system",foreground=UI["accent"],spacing1=8,spacing3=8,font=("Segoe UI",10,"bold"))
        self.chat.tag_config("assistant",foreground=UI["text"],spacing1=4,spacing3=8)
        self.chat.tag_config("user",foreground=UI["accent_2"],spacing1=4,spacing3=8,font=("Segoe UI",10,"bold"))
        self.chat.configure(state="disabled")

        composer=self._glass(chat_panel,bg=UI["glass_alt"])
        composer.pack(fill="x",padx=14,pady=(0,12))
        self._pill(composer,"+",lambda:self._open_lane_popup("local_code"),width=42,height=34,kind="gold").pack(side="left",padx=(10,8),pady=8)
        entry=tk.Entry(composer,textvariable=self.request_var,bg=UI["surface"],fg=UI["text"],insertbackground=UI["text"],relief="flat",bd=0,font=("Segoe UI",10))
        entry.pack(side="left",fill="x",expand=True,ipady=9,pady=8)
        entry.bind("<Return>",lambda e:self._submit_request_prompt())
        output_select=ttk.Combobox(composer,values=["Output selector","ChatGPT ZIP","Gemini readable ZIP","Topology"],state="readonly",width=16)
        output_select.current(0)
        output_select.pack(side="left",padx=8,pady=8)
        self._pill(composer,"Sub-Model AI",lambda:None,width=118,height=34).pack(side="left",padx=(0,10),pady=8)

        lower=self._glass(body,bg=UI["panel"])
        body.add(lower,weight=2)
        self.nb=ttk.Notebook(lower)
        self.nb.pack(fill="both",expand=True,padx=8,pady=8)
        for tab in TAB_ORDER:
            self.ensure_tab(tab)

    def _build_task_status(self, parent):
        prog=self._glass(parent,bg=UI["panel"],line=UI["line"])
        prog.pack(fill="x",padx=16,pady=(0,8))
        self.progress=ttk.Progressbar(prog,mode="determinate",maximum=100,style="Brain.Horizontal.TProgressbar")
        self.progress.pack(fill="x",padx=14,pady=(12,6))
        self.process_var=tk.StringVar(value="Current task: idle")
        self.file_var=tk.StringVar(value="Current file: none")
        self.count_var=tk.StringVar(value="Progress: 0/0 (0%)")
        self.elapsed_var=tk.StringVar(value="Elapsed: 0s")
        self.eta_var=tk.StringVar(value="ETA: --")
        self.finish_var=tk.StringVar(value="Finish: --")
        line=tk.Frame(prog,bg=UI["panel"])
        line.pack(fill="x",padx=14,pady=(0,8))
        for v in [self.process_var,self.file_var,self.count_var,self.elapsed_var,self.eta_var,self.finish_var]:
            tk.Label(line,textvariable=v,bg=UI["panel"],fg=UI["muted"],font=("Segoe UI",9),anchor="w").pack(side="left",fill="x",expand=True,padx=(0,10))
        metrics=tk.Frame(prog,bg=UI["panel"])
        metrics.pack(fill="x",padx=10,pady=(0,10))
        for key in ["CPU","GPU","RAM","SSD/HDD","IO"]:
            var=tk.StringVar(value="unavailable")
            self.metric_vars[key]=var
            tile=self._glass(metrics,bg=UI["glass_alt"])
            tile.pack(side="left",fill="x",expand=True,padx=4)
            tk.Label(tile,text=key,font=("Segoe UI",9,"bold"),bg=UI["glass_alt"],fg=UI["text"]).pack(anchor="w",padx=10,pady=(7,0))
            tk.Label(tile,textvariable=var,font=("Segoe UI",9),bg=UI["glass_alt"],fg=UI["accent"]).pack(anchor="w",padx=10,pady=(0,7))

    def _source_pill_specs(self, compact=False):
        specs=[
            ("GitHub","github"),
            ("Local Code","local_code"),
            ("Chat Lineage","chat_lineage"),
            ("Docs","docs"),
            ("Data/Excel","data_excel_csv"),
            ("PPT","ppt_presentation"),
            ("PDF/OCR","pdf_ocr"),
            ("Images/OCR","images_ocr"),
            ("Artifacts","artifacts"),
            ("Custom","custom"),
        ]
        if compact:
            return specs
        return specs + [("Analysis","analysis"),("Plan","plan"),("Mode","mode"),("Discussion","discussion")]

    def _open_lane_popup(self, lane_key):
        lane=LANE_DEFS.get(lane_key, LANE_DEFS["custom"])
        win=tk.Toplevel(self)
        win.title(f"Evidence OS - {lane['label']} intake")
        win.geometry("520x320")
        win.configure(bg=UI["bg"])
        card=self._glass(win,bg=UI["glass"],line=UI["line"])
        card.pack(fill="both",expand=True,padx=16,pady=16)
        tk.Label(card,text=f"{lane['label']} source intake",font=("Segoe UI",16,"bold"),bg=UI["glass"],fg=UI["text"]).pack(anchor="w",padx=18,pady=(16,4))
        tk.Label(card,text="This pill adds evidence to the selected SQLite Builder brain. File or folder pickers open only after you choose an intake action.",font=("Segoe UI",10),wraplength=462,justify="left",bg=UI["glass"],fg=UI["muted"]).pack(anchor="w",padx=18,pady=(0,12))
        actions=tk.Frame(card,bg=UI["glass"])
        actions.pack(fill="x",padx=16,pady=8)

        def close_then(fn):
            win.destroy()
            fn()

        if lane_key == "github":
            self._pill(actions,"Clone / Pull GitHub",lambda:close_then(self.add_github),width=168,height=34,kind="gold").pack(side="left",padx=4,pady=4)
        elif lane_key == "local_code":
            self._pill(actions,"Select Code Folder",lambda:close_then(self.add_local_code),width=156,height=34,kind="gold").pack(side="left",padx=4,pady=4)
        elif lane_key == "custom":
            self._pill(actions,"Define Custom Lane",lambda:close_then(self.add_custom_lane),width=156,height=34,kind="gold").pack(side="left",padx=4,pady=4)
        else:
            self._pill(actions,"Select Files",lambda k=lane_key:close_then(lambda:self.load_lane_file(k)),width=124,height=34,kind="gold").pack(side="left",padx=4,pady=4)
            self._pill(actions,"Paste Text",lambda k=lane_key:close_then(lambda:self._paste_lane_text(k)),width=118,height=34).pack(side="left",padx=4,pady=4)
        self._pill(actions,"Open Queue",lambda t=lane["tab"]:close_then(lambda:self.select_tab(t)),width=118,height=34).pack(side="left",padx=4,pady=4)
        self._pill(card,"Close",win.destroy,width=90,height=32).pack(anchor="e",padx=18,pady=(14,0))

    def _paste_lane_text(self, lane_key):
        lane=LANE_DEFS.get(lane_key, LANE_DEFS["custom"])
        txt=simpledialog.askstring(f"Paste {lane['label']} source","Paste small text source") or ""
        if txt.strip():
            self._add_source(lane_key,"Paste Text",text=txt,display_name=f"{lane['label']} pasted text")

    def _submit_request_prompt(self):
        text=self.request_var.get().strip()
        if not text:
            return
        self.request_var.set("")
        self._append_chat("user","Request Prompt",text)
        self._append_chat("assistant","Route","SQLite Builder captured this request as visible session context. Build/export actions remain explicit button actions.")

    def _module_placeholder(self, name):
        self._append_chat("assistant",f"{name} pill",f"{name} is visible as an Evidence OS module route. SQLite Builder remains the active functional module.")
        self.status.set(f"{name} route is visual only in this EXE.")

    def _open_admin_placeholder(self):
        self._append_chat("assistant","Admin Cockpit","Escape route is visible. Admin Cockpit is not mutating state in this SQLite Builder EXE.")
        self.status.set("Admin Cockpit route is visual only.")

    def _show_project_chats(self):
        win=tk.Toplevel(self)
        win.title("Evidence OS - Project Chats")
        win.geometry("360x260")
        win.configure(bg=UI["bg"])
        card=self._glass(win,bg=UI["glass"])
        card.pack(fill="both",expand=True,padx=14,pady=14)
        tk.Label(card,text="Project chats",font=("Segoe UI",14,"bold"),bg=UI["glass"],fg=UI["text"]).pack(anchor="w",padx=14,pady=(12,8))
        for name in [self.brain_name.get() or "New Brain","SQLite Builder build chat","Topology/export receipts"]:
            self._pill(card,name,lambda n=name:self._select_brain_name(n) if n == self.brain_name.get() else None,width=290,height=32).pack(anchor="w",padx=14,pady=4)

    def _pin_active_brain(self):
        name=self.brain_name.get().strip() or "New Brain"
        self.pinned_brains.add(name)
        self._render_brain_rows(name)
        self.status.set(f"Pinned brain chat: {name}")

    def _unpin_active_brain(self):
        name=self.brain_name.get().strip() or "New Brain"
        self.pinned_brains.discard(name)
        self._render_brain_rows(name)
        self.status.set(f"Unpinned brain chat: {name}")

    def _remove_active_brain(self):
        name=self.brain_name.get().strip() or "New Brain"
        self.brain_names=[n for n in self.brain_names if n != name]
        self.clear_all_tabs()
        self.sources=[]
        self.brain_name.set(self.brain_names[0] if self.brain_names else "New Brain")
        self._render_brain_rows(self.brain_name.get())
        self._refresh_brain_detail()
        self._sync_session_labels()
        self.status.set(f"Removed {name} from the visible chat list. Disk files were not deleted.")

    def _sync_session_labels(self):
        name=self.brain_name.get().strip() or "New Brain"
        if hasattr(self,"active_chat_var"):
            self.active_chat_var.set(f"{name} / SQLite Builder")
        if hasattr(self,"output_dir_var"):
            try:
                out=brain_output_dir(self.workspace_dir.get(), name).name
            except Exception:
                out=f"{name}_output"
            self.output_dir_var.set(f"Output folder: {out}")

    def _brain_root_for_name(self, name):
        root=normalize_workspace_dir(self.workspace_dir.get())
        planned=brain_output_dir(root, name)
        if (planned/"project"/"project_router.sqlite").exists():
            return planned
        slug=str(planned.name)
        legacy=root/(slug[:-7] if slug.lower().endswith("_output") else slug)
        if (legacy/"project"/"project_router.sqlite").exists():
            return legacy
        return planned

    def _display_name_for_brain_dir(self, folder: Path) -> str:
        router=folder/"project"/"project_router.sqlite"
        if router.exists():
            try:
                con=sqlite3.connect(router)
                row=con.execute("SELECT brain_name FROM brain_manifest LIMIT 1").fetchone()
                con.close()
                if row and row[0]:
                    return str(row[0])
            except Exception:
                pass
        name=folder.name
        if name.lower().endswith("_output"):
            name=name[:-7]
        return name or "New Brain"

    def _refresh_metrics(self):
        try:
            data=self.metric_sampler.snapshot(self.workspace_dir.get())
            for key,var in self.metric_vars.items():
                var.set(data.get(key,"unavailable"))
        except Exception:
            for var in self.metric_vars.values():
                var.set("unavailable")
        self.after(1500,self._refresh_metrics)

    def _style(self):
        style=ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TNotebook",background=UI["bg"],borderwidth=0)
        style.configure("TNotebook.Tab",background=UI["panel_2"],foreground=UI["muted"],padding=(12,6),font=("Segoe UI",9,"bold"))
        style.map("TNotebook.Tab",background=[("selected",UI["button_hover"])],foreground=[("selected",UI["text"])])
        style.configure("Treeview",background=UI["panel_2"],fieldbackground=UI["panel_2"],foreground=UI["text"],rowheight=28,borderwidth=0,font=("Segoe UI",9))
        style.configure("Treeview.Heading",background=UI["button"],foreground=UI["text"],font=("Segoe UI",9,"bold"),relief="flat")
        style.map("Treeview",background=[("selected",UI["button_hover"])],foreground=[("selected",UI["text"])])
        style.configure("Vertical.TScrollbar",background=UI["panel_2"],troughcolor=UI["bg"],bordercolor=UI["line"],arrowcolor=UI["muted"])
        style.configure("Brain.Horizontal.TProgressbar",background=UI["accent"],troughcolor=UI["panel_2"],bordercolor=UI["line"],lightcolor=UI["accent"],darkcolor=UI["accent"])

    def _pill(self, parent, text, command, width=136, height=34, kind=None):
        if kind == "gold":
            fill=UI["button_gold"]; hover=UI["button_gold_hover"]; outline=UI["accent_2"]
        elif kind == "active":
            fill=UI["button_active"]; hover=UI["button_active_hover"]; outline=UI["accent"]
        elif kind == "danger":
            fill=UI["button_danger"]; hover=UI["button_danger_hover"]; outline=UI["danger"]
        else:
            fill=UI["button"]; hover=UI["button_hover"]; outline=UI["line"]
        return PillButton(parent,text,command=command,width=width,height=height,fill=fill,hover=hover,outline=outline)

    def _bind_wheel(self, canvas):
        def _on_enter(event):
            canvas.bind_all("<MouseWheel>", _on_mousewheel)
        def _on_leave(event):
            canvas.unbind_all("<MouseWheel>")
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind("<Enter>", _on_enter)
        canvas.bind("<Leave>", _on_leave)

    def toggle_sidebar(self):
        if not hasattr(self, "sidebar"):
            return
        if hasattr(self, "sidebar_full") and hasattr(self, "sidebar_rail"):
            if self.sidebar_collapsed:
                self.sidebar_rail.pack_forget()
                self.sidebar.configure(width=354)
                self.sidebar_full.pack(fill="both",expand=True)
                self.sidebar_collapsed=False
                self._append_chat("assistant","Sidebar expanded","Project chats, active chat, M-Loader, and source pills are visible again.")
            else:
                self.sidebar_full.pack_forget()
                self.sidebar.configure(width=108)
                self.sidebar_rail.pack(fill="both",expand=True)
                self.sidebar_collapsed=True
                self._append_chat("assistant","Sidebar collapsed","Evidence OS rail mode is active.")
            return
        if self.sidebar_collapsed:
            self.sidebar.configure(width=330)
            if hasattr(self, "sidebar_toggle_pill"):
                self.sidebar_toggle_pill.set_text("Hide")
            if hasattr(self, "_sidebar_pack_cache"):
                for widget, info in self._sidebar_pack_cache:
                    try:
                        widget.pack(**info)
                    except Exception:
                        pass
            self.sidebar_collapsed = False
            self._append_chat("assistant","Sidebar expanded","Brain chats and lane pills are visible again.")
        else:
            widgets = self.sidebar.winfo_children()[2:]
            self._sidebar_pack_cache = []
            for widget in widgets:
                if widget.winfo_manager() == "pack":
                    self._sidebar_pack_cache.append((widget, widget.pack_info()))
                    widget.pack_forget()
            self.sidebar.configure(width=112)
            if hasattr(self, "sidebar_toggle_pill"):
                self.sidebar_toggle_pill.set_text("Show")
            self.sidebar_collapsed = True
            self._append_chat("assistant","Sidebar collapsed","The side rail is hidden. Use Hide again to expand it.")

    def _append_chat(self, role, title, body):
        if not hasattr(self,"chat"):
            return
        self.chat.configure(state="normal")
        tag="system" if role=="system" else "user" if role=="user" else "assistant"
        self.chat.insert("end", f"{title}\n", tag)
        self.chat.insert("end", f"{body}\n\n", "assistant" if role!="user" else "user")
        self.chat.see("end")
        self.chat.configure(state="disabled")

    def ensure_tab(self, tab):
        if tab in self.tabs: return self.trees[tab]
        f=ttk.Frame(self.nb); self.nb.add(f,text=tab)
        cols=("source","lane","source_type","hash","status","active")
        tree=ttk.Treeview(f,columns=cols,show="headings",selectmode="extended")
        for c in cols:
            tree.heading(c,text=c); tree.column(c,width=260 if c=="source" else 140)
        tree.pack(fill="both",expand=True)
        self.tabs[tab]=f; self.trees[tab]=tree; return tree

    def select_tab(self, tab):
        self.ensure_tab(tab); self.nb.select(self.tabs[tab]); self.status.set(f"Lane selected: {tab}")
        self._append_chat("user","Lane selected",f"{tab} source queue is active.")

    def clear_all_tabs(self):
        for tree in self.trees.values():
            for item in tree.get_children():
                tree.delete(item)

    def refresh_brain_list(self):
        current=self.brain_name.get()
        root=normalize_workspace_dir(self.workspace_dir.get())
        names=[]
        if root.exists():
            for p in sorted(root.iterdir()):
                if p.is_dir() and (p/"project"/"project_router.sqlite").exists():
                    names.append(self._display_name_for_brain_dir(p))
        if "new_brain" not in [n.lower() for n in names] and not names:
            names=["New Brain"]
        pinned=[n for n in self.pinned_brains if n not in names]
        self.brain_names = pinned + names
        self._render_brain_rows(current)
        if hasattr(self,"brain_detail_var"):
            self._refresh_brain_detail()
        self._sync_session_labels()

    def _brain_selected(self, event=None):
        return

    def _select_brain_name(self, name):
        self.brain_name.set(name)
        self.load_brain_state(name)
        self._append_chat("user","Brain selected",name)
        self._render_brain_rows(name)
        self._sync_session_labels()

    def _brain_source_count(self, name):
        router=self._brain_root_for_name(name)/"project"/"project_router.sqlite"
        if not router.exists():
            return 0
        try:
            con=sqlite3.connect(router)
            count=con.execute("SELECT COUNT(*) FROM source_registry WHERE active_bool=1").fetchone()[0]
            con.close()
            return count
        except Exception:
            return 0

    def _render_brain_rows(self, current=None):
        if not hasattr(self, "brain_rows"):
            return
        for child in self.brain_rows.winfo_children():
            child.destroy()
        current=(current or self.brain_name.get() or "").lower()
        for name in self.brain_names:
            active=name.lower()==current
            bg=UI["brain_card_active"] if active else UI["brain_card"]
            row=tk.Frame(self.brain_rows,bg=bg,highlightthickness=1,highlightbackground=UI["accent"] if active else UI["line_soft"],cursor="hand2")
            row.pack(fill="x",padx=8,pady=6)
            dot=tk.Label(row,text="o",bg=bg,fg=UI["ok"] if self._brain_source_count(name) else UI["muted"],font=("Segoe UI",12,"bold"))
            dot.pack(side="left",padx=(10,6),pady=9)
            text=tk.Frame(row,bg=bg)
            text.pack(side="left",fill="x",expand=True,pady=7)
            pin="PIN " if name in self.pinned_brains else ""
            tk.Label(text,text=f"{pin}{name}",bg=bg,fg=UI["text"],font=("Segoe UI",10,"bold"),anchor="w").pack(fill="x")
            tk.Label(text,text=f"{self._brain_source_count(name)} active source(s) / SQLite Builder",bg=bg,fg=UI["muted"],font=("Segoe UI",8),anchor="w").pack(fill="x")
            for widget in (row,dot,text,*text.winfo_children()):
                widget.bind("<Button-1>",lambda e,n=name:self._select_brain_name(n))

    def _refresh_brain_detail(self):
        active=sum(1 for s in self.sources if s.get("active",True))
        lanes=sorted({LANE_DEFS.get(s.get("lane_key"), LANE_DEFS["custom"])["label"] for s in self.sources})
        lane_text=", ".join(lanes[:5]) + ("..." if len(lanes)>5 else "")
        if not lane_text:
            lane_text="no sources yet"
        self.brain_detail_var.set(f"{self.brain_name.get()}\nactive sources: {active}\nlanes: {lane_text}")

    def load_brain_state(self, name):
        self.sources=[]
        self.clear_all_tabs()
        root=self._brain_root_for_name(name)
        router=root/"project"/"project_router.sqlite"
        if router.exists():
            try:
                con=sqlite3.connect(router)
                rows=con.execute("SELECT source_id,lane_key,lane_label,source_type,display_name,path,source_hash,active_bool FROM source_registry ORDER BY created_at, display_name").fetchall()
                con.close()
                for sid,lane_key,lane_label,stype,display,path,sha,active in rows:
                    lane=LANE_DEFS.get(lane_key, LANE_DEFS["custom"]); tab=lane["tab"]
                    src={"source_id":sid,"lane_key":lane_key,"source_type":stype,"path":path,"display_name":display or path,"active":bool(active)}
                    self.sources.append(src)
                    self.ensure_tab(tab).insert("","end",values=(src["display_name"],lane["label"],stype,sha or "", "LOADED_FROM_BRAIN", "YES" if active else "NO"))
                self.status.set(f"Brain loaded: {name} / {len(self.sources)} source(s)")
                self._refresh_brain_detail()
            except Exception as e:
                self.status.set(f"Brain load failed: {e}")
                self._append_chat("system","Brain load failed",str(e))
        else:
            self.status.set(f"New/unbuilt brain selected: {name}")
            self._refresh_brain_detail()
        self._sync_session_labels()

    def choose_workspace(self):
        f=filedialog.askdirectory(title="Select workspace root")
        if f:
            self.workspace_dir.set(str(normalize_workspace_dir(f)))
            self.refresh_brain_list()
            self.clear_all_tabs(); self.sources=[]
            self._sync_session_labels()

    def new_brain(self):
        name=self.brain_name.get().strip() or "New Brain"
        if name not in self.brain_names:
            self.brain_names.append(name)
            self._render_brain_rows(name)
        self.clear_all_tabs(); self.sources=[]
        self.status.set(f"New brain active: {name}")
        self._refresh_brain_detail()
        self._sync_session_labels()
        self._append_chat("assistant","New brain ready",f"{name} is ready for source intake.")

    def open_output_folder(self):
        root=brain_output_dir(self.workspace_dir.get(), self.brain_name.get()); root.mkdir(parents=True,exist_ok=True); os.startfile(str(root))

    def _add_source(self,lane_key,source_type,path="",text="",schema_contract="",display_name="", extra=None):
        lane=LANE_DEFS.get(lane_key, LANE_DEFS["custom"]); tab=lane["tab"]
        src={"source_id":f"source_{len(self.sources)+1}","lane_key":lane_key,"source_type":source_type,"path":path,"text":text,"schema_contract":schema_contract,"display_name":display_name or path or lane["label"],"active":True}
        if extra: src.update(extra)
        self.sources.append(src)
        self.ensure_tab(tab).insert("","end",values=(src["display_name"],lane["label"],source_type,"pending","REGISTERED","YES"))
        self.select_tab(tab)
        self._refresh_brain_detail()
        self._append_chat("user","Source added",f"{lane['label']}: {src['display_name']}")

    def add_local_code(self):
        f=filedialog.askdirectory(title="Select local coded project folder")
        if f: self._add_source("local_code","Local Code Folder",path=f,display_name=f)

    def add_github(self):
        win=tk.Toplevel(self); win.title("GitHub Pull / Clone Options"); win.geometry("680x360")
        url=tk.StringVar(); branch=tk.StringVar(value=""); token=tk.StringVar(value=""); target_name=tk.StringVar(value="")
        full_history=tk.BooleanVar(value=True); pull_existing=tk.BooleanVar(value=True)
        rows=[("Repo URL",url), ("Branch/tag (blank=default)",branch), ("Token optional (not stored)",token), ("Local folder name optional",target_name)]
        for i,(label,var) in enumerate(rows):
            ttk.Label(win,text=label).grid(row=i,column=0,sticky="w",padx=8,pady=5)
            ttk.Entry(win,textvariable=var,show="*" if "Token" in label else "").grid(row=i,column=1,sticky="ew",padx=8,pady=5)
        ttk.Checkbutton(win,text="Full history clone/pull",variable=full_history).grid(row=4,column=0,sticky="w",padx=8,pady=5)
        ttk.Checkbutton(win,text="Pull if repo already exists",variable=pull_existing).grid(row=4,column=1,sticky="w",padx=8,pady=5)
        win.columnconfigure(1,weight=1)
        def run_clone():
            u=url.get().strip(); br=branch.get().strip(); tok=token.get().strip()
            if not u: messagebox.showerror("Missing URL","Repo URL required"); return
            clone_url=u
            if tok and u.startswith("https://") and "@" not in u.split("//",1)[1].split("/",1)[0]:
                clone_url="https://"+tok+"@"+u.split("https://",1)[1]
            name=target_name.get().strip() or (u.rstrip('/').split('/')[-1].replace('.git','') or 'repo')
            if br: name=name+"_"+br.replace('/','_')
            staging=normalize_workspace_dir(self.workspace_dir.get())/"github_staging"; staging.mkdir(parents=True,exist_ok=True)
            target=staging/name
            try:
                if target.exists() and (target/'.git').exists() and pull_existing.get():
                    cmd=["git","-C",str(target),"pull","--ff-only"]
                    run_hidden(cmd,check=True)
                    if br: run_hidden(["git","-C",str(target),"checkout",br],check=True)
                else:
                    cmd=["git","clone"]
                    if br: cmd += ["--branch",br]
                    if not full_history.get(): cmd += ["--depth","1"]
                    cmd += [clone_url,str(target)]
                    run_hidden(cmd,check=True)
                self._add_source("github","GitHub Repo",path=str(target),display_name=(u+(" @ "+br if br else "")), extra={"repo_url":u,"branch":br,"full_history":full_history.get()})
                win.destroy()
            except Exception as e:
                messagebox.showerror("GitHub failed",str(e))
        ttk.Button(win,text="Clone / Pull and Add as Code Lane",command=run_clone).grid(row=5,column=0,columnspan=2,pady=15)

    def load_lane_file(self,lane_key):
        lane=LANE_DEFS[lane_key]
        files=filedialog.askopenfilenames(
            title=f"Select {lane['label']} file(s)",
            filetypes=lane.get("filetypes") or _CLASSIFIED_FALLBACK_FILETYPES["custom"],
        )
        for f in files: self._add_source(lane_key,"File / Document",path=f,display_name=Path(f).name)

    def add_custom_lane(self):
        name=simpledialog.askstring("Custom Lane","Custom lane name")
        if not name: return
        key=name.lower().replace(" ","_").replace("/","_")
        win=tk.Toplevel(self); win.title(f"Schema for custom lane: {name}"); win.geometry("650x430")
        ttk.Label(win,text="Enter schema contract first. One field/table per line. This is mandatory for custom lane.").pack(anchor="w",padx=8,pady=5)
        box=tk.Text(win,height=16); box.pack(fill="both",expand=True,padx=8,pady=5)
        box.insert("1.0","custom_source\ncustom_item\ncustom_evidence\ncustom_decision\ncustom_next_action\ncustom_fts")
        def choose_files():
            schema=box.get("1.0",tk.END).strip()
            if not schema: messagebox.showerror("Schema required","Custom lane schema is mandatory"); return
            files=filedialog.askopenfilenames(
                title=f"Select files for {name}",
                filetypes=_CLASSIFIED_FALLBACK_FILETYPES["custom"],
            )
            for f in files: self._add_source(key,"File / Document",path=f,schema_contract=schema,display_name=Path(f).name)
            win.destroy()
        def paste_text():
            schema=box.get("1.0",tk.END).strip()
            if not schema: messagebox.showerror("Schema required","Custom lane schema is mandatory"); return
            txt=simpledialog.askstring("Paste custom source","Paste small text source") or ""
            self._add_source(key,"Paste Text",text=txt,schema_contract=schema,display_name=name+": pasted_text")
            win.destroy()
        ttk.Button(win,text="Continue to File Intake",command=choose_files).pack(side="left",padx=8,pady=8)
        ttk.Button(win,text="Paste Text Source",command=paste_text).pack(side="left",padx=8,pady=8)

    def scan_tools(self):
        self.started_at=time.time()
        if hasattr(self,"brain_pulse"):
            self.brain_pulse.start()
        self.progress["value"]=8
        self.process_var.set("Current task: scanning tools")
        self._append_chat("system","Tool scanner started","Evidence OS is checking local SQLite Builder tool availability.")
        def run():
            try:
                result=runtime_scan_tools()
                summary=json.dumps(result,indent=2)
                self.q.put({"stage":"done","task":"tool scan complete","file":summary[:500],"percent":100,"done":1,"total":1,"eta_seconds":"--","finish_epoch":"--","tool_scan":summary})
            except Exception as e:
                self.q.put({"stage":"failed","task":f"tool scan failed: {e}","file":"","percent":0,"done":0,"total":0,"eta_seconds":0,"finish_epoch":"--"})
        threading.Thread(target=run,daemon=True).start()
    def install_deps(self): self._start(lambda: runtime_install_deps(self._progress), "installing parser dependencies")
    def preview(self):
        rows=[]
        for tab,tree in self.trees.items():
            for item in tree.selection(): rows.append((tab,tree.item(item,"values")))
        messagebox.showinfo("Impact Preview", "No rows selected." if not rows else "\n".join([f"{t}: {v[0]}" for t,v in rows[:40]]))
    def unload_selected(self):
        removed=0
        for tab,tree in self.trees.items():
            for item in list(tree.selection()):
                vals=tree.item(item,"values"); display=vals[0]
                for s in self.sources:
                    if s.get("display_name")==display: s["active"]=False
                tree.delete(item); removed+=1
        self.status.set(f"Unloaded {removed} selected source(s). Click BUILD BRAIN to refresh active brain.")
        self._refresh_brain_detail()
        self._append_chat("assistant","Source queue updated",f"Unloaded {removed} selected source(s).")
    def build_brain(self):
        if not self.sources: messagebox.showwarning("No sources","Add at least one source."); return
        name=self.brain_name.get().strip() or "New Brain"
        if name not in self.brain_names:
            self.brain_names.append(name)
            self._render_brain_rows(name)
        self._start(lambda: runtime_build_brain(self.workspace_dir.get(), name, self.sources, self._progress), "building brain")
    def render(self): self._start(lambda: render_topology(self.workspace_dir.get(), self.brain_name.get(), self._progress), "rendering topology")
    def export(self):
        self._start(lambda: export_one_upload_package(self.workspace_dir.get(), self.brain_name.get(), self._progress), "exporting one-upload package")

    def gemini(self):
        self._start(lambda: export_gemini_exact10(self.workspace_dir.get(), self.brain_name.get(), self._progress), "exporting Gemini provider-readable package")
    def _start(self, fn, label):
        self.started_at=time.time(); self.progress["value"]=0; self.process_var.set("Current process: "+label)
        if hasattr(self,"brain_pulse"):
            self.brain_pulse.start()
        self._append_chat("system","Process started",label)
        def run():
            try:
                result=fn()
                self.q.put({"stage":"done","task":label+" complete","file":str(result)[:500],"percent":100,"done":1,"total":1,"eta_seconds":"--","finish_epoch":"--"})
            except Exception as e: self.q.put({"stage":"failed","task":str(e),"file":"","percent":0,"done":0,"total":0,"eta_seconds":0,"finish_epoch":"--"})
        threading.Thread(target=run,daemon=True).start()
    def view_flash_prompt(self):
        win = tk.Toplevel(self)
        win.title("View Flash prompt")
        win.geometry("920x720")
        text = tk.Text(win, wrap="word")
        text.insert("1.0", LOCKED_FLASH_PROMPT)
        text.configure(state="disabled")
        text.pack(fill="both", expand=True, padx=8, pady=8)

        def copy_prompt():
            self.clipboard_clear()
            self.clipboard_append(LOCKED_FLASH_PROMPT)
            self.status.set("Flash prompt copied to clipboard.")

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(bar, text="Copy", command=copy_prompt).pack(side="left")
        ttk.Button(bar, text="Close", command=win.destroy).pack(side="right")


    def _progress(self, ev): self.q.put(ev)
    def _poll(self):
        try:
            while True:
                ev=self.q.get_nowait(); pct=int(ev.get("percent",0)); self.progress["value"]=pct
                self.process_var.set(f"Current process: {ev.get('stage','')} / {ev.get('task','')}")
                self.file_var.set(f"Current file: {ev.get('file','') or 'none'}")
                done=ev.get("done",0); total=ev.get("total",0); self.count_var.set(f"Progress: {done}/{total} ({pct}%)")
                self.eta_var.set(f"ETA: {ev.get('eta_seconds','--')}s")
                self.finish_var.set(f"Estimated finish epoch: {ev.get('finish_epoch','--')}")
                self.status.set(f"{ev.get('stage','')}: {ev.get('task','')}")
                if ev.get("stage") == "done":
                    if hasattr(self,"brain_pulse"):
                        self.brain_pulse.stop()
                    self._append_chat("assistant","Receipt",f"{ev.get('task','complete')}\\n{ev.get('file','')}")
                    if ev.get("tool_scan"):
                        self._append_chat("assistant","Tool scanner",ev.get("tool_scan","")[:1800])
                    self.refresh_brain_list()
                    self._refresh_brain_detail()
                elif ev.get("stage") == "failed":
                    if hasattr(self,"brain_pulse"):
                        self.brain_pulse.stop()
                    self._append_chat("system","Process failed",ev.get("task","unknown failure"))
        except queue.Empty: pass
        self.after(100,self._poll)
    def _tick(self):
        if self.started_at: self.elapsed_var.set(f"Elapsed: {int(time.time()-self.started_at)}s")
        self.after(1000,self._tick)


def main():
    app=MainWindow(); app.mainloop()
