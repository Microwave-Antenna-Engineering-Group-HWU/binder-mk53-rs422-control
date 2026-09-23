"""Convert MK53_CONNECTION_REPORT.md to MK53_CONNECTION_REPORT.pdf.

Run from the repository root with the project environment (needs reportlab and Pillow):
    .venv\\Scripts\\python.exe docs\\build_report_pdf.py
"""

import io
import os
import re
from urllib.parse import unquote

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable, Image, KeepTogether, Paragraph, Preformatted,
    SimpleDocTemplate, Spacer, Table, TableStyle,
)

PAGE_W, PAGE_H = A4
ACCENT    = colors.HexColor('#1a3a6e')
ACCENT_LT = colors.HexColor('#dce6f5')
CODE_BG   = colors.HexColor('#f4f4f4')
RULE_CLR  = colors.HexColor('#aaaaaa')

HERE     = os.path.dirname(os.path.abspath(__file__))
MD_PATH  = os.path.join(HERE, 'MK53_CONNECTION_REPORT.md')
PDF_PATH = os.path.join(HERE, 'MK53_CONNECTION_REPORT.pdf')

HEADER_L = 'MK 53 E2 Climate Chamber: Computer Connection'
FOOTER_L = 'Heriot-Watt University lab equipment record'
FOOTER_R = '23 September 2026'

IMG_MAX_PX = 1600     # downscale photos so the PDF stays small
IMG_MAX_H  = 7 * cm

BODY_F = 'Helvetica'
BOLD_F = 'Helvetica-Bold'
ITAL_F = 'Helvetica-Oblique'
MONO_F = 'Courier'

try:
    pdfmetrics.registerFont(TTFont('Calibri',        r'C:\Windows\Fonts\calibri.ttf'))
    pdfmetrics.registerFont(TTFont('Calibri-Bold',   r'C:\Windows\Fonts\calibrib.ttf'))
    pdfmetrics.registerFont(TTFont('Calibri-Italic', r'C:\Windows\Fonts\calibrii.ttf'))
    pdfmetrics.registerFontFamily('Calibri', normal='Calibri',
                                  bold='Calibri-Bold', italic='Calibri-Italic')
    BODY_F, BOLD_F, ITAL_F = 'Calibri', 'Calibri-Bold', 'Calibri-Italic'
except Exception:
    pass

try:
    pdfmetrics.registerFont(TTFont('Consolas', r'C:\Windows\Fonts\consola.ttf'))
    MONO_F = 'Consolas'
except Exception:
    pass

styles = getSampleStyleSheet()

H1 = ParagraphStyle('pH1', parent=styles['Normal'], fontName=BOLD_F, fontSize=22,
                    leading=26, textColor=ACCENT, spaceAfter=6, spaceBefore=0)
H2 = ParagraphStyle('pH2', parent=styles['Normal'], fontName=BOLD_F, fontSize=14,
                    textColor=ACCENT, spaceBefore=20, spaceAfter=4)
H3 = ParagraphStyle('pH3', parent=styles['Normal'], fontName=BOLD_F, fontSize=11,
                    textColor=colors.HexColor('#222222'), spaceBefore=12, spaceAfter=3)
BODY = ParagraphStyle('pBody', parent=styles['Normal'], fontName=BODY_F,
                      fontSize=10, leading=15, spaceAfter=3)
BULL = ParagraphStyle('pBull', parent=styles['Normal'], fontName=BODY_F,
                      fontSize=10, leading=14, spaceAfter=2, leftIndent=22)
META = ParagraphStyle('pMeta', parent=styles['Normal'], fontName=BODY_F,
                      fontSize=9.5, leading=14,
                      textColor=colors.HexColor('#444444'), spaceAfter=2)
CAPT = ParagraphStyle('pCapt', parent=styles['Normal'], fontName=ITAL_F,
                      fontSize=8.5, leading=12, alignment=1,
                      textColor=colors.HexColor('#444444'), spaceAfter=8)
TH_S = ParagraphStyle('pTH', parent=styles['Normal'], fontName=BOLD_F,
                      fontSize=9, leading=12, textColor=colors.white)
TD_S = ParagraphStyle('pTD', parent=styles['Normal'], fontName=BODY_F,
                      fontSize=9, leading=12)
CODE_S = ParagraphStyle('pCode', parent=styles['Normal'], fontName=MONO_F,
                        fontSize=7.5, leading=10.5)

AVAIL_W = PAGE_W - 4 * cm


def esc(t):
    return t.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def fmt(t):
    t = esc(t)
    t = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', t)
    t = re.sub(r'`(.+?)`', r'<font name="{}" size="8">\1</font>'.format(MONO_F), t)
    return t


def make_table(rows):
    n_cols = max(len(r) for r in rows)
    # column widths follow the amount of text, within sensible limits
    weights = [min(max(len(r[c]) if c < len(r) else 0 for r in rows), 60)
               for c in range(n_cols)]
    weights = [max(w, 10) for w in weights]
    col_w = [AVAIL_W * w / sum(weights) for w in weights]

    tdata = [[Paragraph(fmt(c), TH_S) for c in rows[0]]]
    for row in rows[1:]:
        cells = list(row) + [''] * (n_cols - len(row))
        tdata.append([Paragraph(fmt(c), TD_S) for c in cells])

    cmd = [
        ('BACKGROUND',    (0, 0), (-1, 0),  ACCENT),
        ('LINEBELOW',     (0, 0), (-1, 0),  1.5, ACCENT),
        ('GRID',          (0, 0), (-1, -1), 0.4, colors.HexColor('#c8c8c8')),
        ('TOPPADDING',    (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING',   (0, 0), (-1, -1), 8),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 8),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
    ]
    for r in range(1, len(tdata)):
        if r % 2 == 0:
            cmd.append(('BACKGROUND', (0, r), (-1, r), ACCENT_LT))

    t = Table(tdata, colWidths=col_w, repeatRows=1)
    t.setStyle(TableStyle(cmd))
    return t


def make_code_block(lines):
    pre = Preformatted('\n'.join(lines), CODE_S)
    inner = Table([[pre]], colWidths=[AVAIL_W - 10])
    inner.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, -1), CODE_BG),
        ('TOPPADDING',    (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING',   (0, 0), (-1, -1), 14),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 8),
        ('BOX',           (0, 0), (-1, -1), 0.5, colors.HexColor('#cccccc')),
        ('LINEBEFORE',    (0, 0), (0, -1),  4,   ACCENT),
    ]))
    return inner


def make_image(alt, target):
    """Photo scaled to the page width (and a height cap), with a caption."""
    path = os.path.join(HERE, unquote(target))
    img = PILImage.open(path).convert('RGB')
    img.thumbnail((IMG_MAX_PX, IMG_MAX_PX))
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=85)
    buf.seek(0)

    w, h = img.size
    scale = min(AVAIL_W / w, IMG_MAX_H / h)
    return KeepTogether([
        Spacer(1, 4),
        Image(buf, width=w * scale, height=h * scale),
        Paragraph(esc(alt), CAPT),
    ])


def on_page(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(RULE_CLR)
    canvas.setLineWidth(0.5)

    canvas.line(2 * cm, PAGE_H - 1.5 * cm, PAGE_W - 2 * cm, PAGE_H - 1.5 * cm)
    canvas.setFont(BOLD_F, 8)
    canvas.setFillColor(ACCENT)
    canvas.drawString(2 * cm, PAGE_H - 1.25 * cm, HEADER_L)

    canvas.line(2 * cm, 1.5 * cm, PAGE_W - 2 * cm, 1.5 * cm)
    canvas.setFont(BODY_F, 8)
    canvas.setFillColor(colors.HexColor('#555555'))
    canvas.drawString(2 * cm, 1.0 * cm, FOOTER_L)
    canvas.drawCentredString(PAGE_W / 2, 1.0 * cm, f'Page {doc.page}')
    canvas.drawRightString(PAGE_W - 2 * cm, 1.0 * cm, FOOTER_R)
    canvas.restoreState()


SEP_RE  = re.compile(r'^\|[-:\s|]+\|?\s*$')
BULL_RE = re.compile(r'^([ \t]*)[-*] (.+)')
IMG_RE  = re.compile(r'^!\[(.*?)\]\((.+?)\)\s*$')
META_RE = re.compile(r'^(Date|Status|Files): ')


def split_row(line):
    """Split a Markdown table row, keeping empty cells."""
    return [c.strip() for c in line.strip().strip('|').split('|')]


def build_story(lines):
    story = []
    i = 0
    while i < len(lines):
        line = lines[i]

        if line.strip().startswith('```'):
            code = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith('```'):
                code.append(lines[i])
                i += 1
            while code and not code[0].strip():
                code.pop(0)
            while code and not code[-1].strip():
                code.pop()
            if code:
                story += [Spacer(1, 4), make_code_block(code), Spacer(1, 8)]
            i += 1
            continue

        if line.startswith('|'):
            rows = []
            while i < len(lines) and lines[i].startswith('|'):
                if not SEP_RE.match(lines[i]):
                    rows.append(split_row(lines[i]))
                i += 1
            if rows:
                story += [Spacer(1, 4), make_table(rows), Spacer(1, 10)]
            continue

        m = IMG_RE.match(line)
        if m:
            story.append(make_image(m.group(1), m.group(2)))
            i += 1
            continue

        if line.startswith('# '):
            story.append(Paragraph(esc(line[2:]), H1))
            story.append(HRFlowable(width='100%', thickness=2.5, color=ACCENT,
                                    spaceAfter=8))
        elif line.startswith('## '):
            story.append(Paragraph(esc(line[3:]), H2))
            story.append(HRFlowable(width='100%', thickness=0.8, color=ACCENT,
                                    spaceAfter=4))
        elif line.startswith('### '):
            story.append(Paragraph(esc(line[4:]), H3))
        elif line.strip() == '---':
            story += [Spacer(1, 8),
                      HRFlowable(width='100%', thickness=0.5, color=RULE_CLR,
                                 spaceAfter=8)]
        elif BULL_RE.match(line):
            m = BULL_RE.match(line)
            style = ParagraphStyle('pB2', parent=BULL,
                                   leftIndent=22 + len(m.group(1)) * 8)
            story.append(Paragraph('\u2022 ' + fmt(m.group(2)), style))
        elif line.strip() == '':
            story.append(Spacer(1, 5))
        elif META_RE.match(line):
            story.append(Paragraph(fmt(line), META))
        else:
            story.append(Paragraph(fmt(line), BODY))
        i += 1
    return story


def main():
    with open(MD_PATH, encoding='utf-8') as f:
        lines = [l.rstrip('\n') for l in f]

    doc = SimpleDocTemplate(
        PDF_PATH, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=2.4 * cm, bottomMargin=2.2 * cm,
        title='MK 53 E2 Climate Chamber: Computer Connection',
        author='Spyros Daskalakis',
        subject='RS 422 connection of the BINDER MK 53 E2 to a Windows PC',
        creator='build_report_pdf.py',
    )
    doc.build(build_story(lines), onFirstPage=on_page, onLaterPages=on_page)
    print('PDF written to:', PDF_PATH)


if __name__ == '__main__':
    main()
