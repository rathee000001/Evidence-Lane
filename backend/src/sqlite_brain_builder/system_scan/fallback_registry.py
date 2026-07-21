FALLBACKS = [
    ('PDF','PyMuPDF','pypdf','metadata + review-required'),
    ('Image','Tesseract OCR','image metadata','review-required hash coverage'),
    ('DOCX','python-docx','unzip XML text extraction','metadata review'),
    ('PPTX','python-pptx','unzip slide XML','metadata review'),
    ('XLSX','openpyxl','XML/sheet metadata extraction','workbook metadata review'),
    ('Git','Git CLI full history','local folder scan without lineage','metadata/review lineage missing'),
    ('MMD','Mermaid CLI SVG','browser renderer if implemented','save MMD + render-blocked receipt'),
]

def as_rows():
    return [{'tool_category':a,'primary_tool':b,'fallback_tool':c,'metadata_only_fallback':d} for a,b,c,d in FALLBACKS]
