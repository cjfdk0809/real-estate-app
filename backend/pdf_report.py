# -*- coding: utf-8 -*-
"""
자산 분석 리포트 PDF 생성 (서버사이드)
- 매 페이지 상단에 키움에프앤아이 CI 머릿말 + 네이비 밑줄 (onPage 콜백 → 브라우저 인쇄와 달리 항상 확실)
- 매 페이지 하단에 페이지 번호 + 고지문
- 한글: NanumGothic (assets/ 번들)
프론트에서 리포트 데이터를 sections 구조로 POST → PDF 바이트 반환.
"""
import os
import io

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, PageTemplate, Frame, Paragraph, Spacer, Table, TableStyle,
)
from reportlab.platypus.flowables import KeepTogether

_HERE = os.path.dirname(os.path.abspath(__file__))


def _asset(name):
    """backend/ 또는 backend/assets/ 어디에 올려도 찾도록 탐색."""
    for base in (_HERE, os.path.join(_HERE, 'assets')):
        p = os.path.join(base, name)
        if os.path.exists(p):
            return p
    return os.path.join(_HERE, name)

NAVY = colors.HexColor('#1A2540')
PINK = colors.HexColor('#E6007E')
INK = colors.HexColor('#22272E')
MUTED = colors.HexColor('#6B7280')
LINE = colors.HexColor('#E5E7EB')

_FONTS_READY = False


def _ensure_fonts():
    global _FONTS_READY
    if _FONTS_READY:
        return
    reg = _asset('NanumGothic.ttf')
    bold = _asset('NanumGothicBold.ttf')
    pdfmetrics.registerFont(TTFont('Nanum', reg))
    if os.path.exists(bold):
        pdfmetrics.registerFont(TTFont('NanumBold', bold))
    else:
        pdfmetrics.registerFont(TTFont('NanumBold', reg))
    _FONTS_READY = True


# ---- 스타일 ----
def _styles():
    return {
        'h1': ParagraphStyle('h1', fontName='NanumBold', fontSize=17, leading=22,
                             textColor=NAVY, spaceAfter=2),
        'sub': ParagraphStyle('sub', fontName='Nanum', fontSize=9, leading=13,
                              textColor=MUTED, spaceAfter=10),
        'h2': ParagraphStyle('h2', fontName='NanumBold', fontSize=12, leading=16,
                             textColor=NAVY, spaceBefore=12, spaceAfter=6),
        'body': ParagraphStyle('body', fontName='Nanum', fontSize=9.5, leading=14,
                              textColor=INK, spaceAfter=4),
        'cell': ParagraphStyle('cell', fontName='Nanum', fontSize=8.5, leading=12,
                              textColor=INK),
        'cellR': ParagraphStyle('cellR', fontName='Nanum', fontSize=8.5, leading=12,
                              textColor=INK, alignment=2),
        'cellH': ParagraphStyle('cellH', fontName='NanumBold', fontSize=8.5, leading=12,
                              textColor=colors.white),
        'kvK': ParagraphStyle('kvK', fontName='Nanum', fontSize=9, leading=13,
                             textColor=MUTED),
        'kvV': ParagraphStyle('kvV', fontName='NanumBold', fontSize=9.5, leading=13,
                             textColor=INK),
    }


# ---- 매 페이지 머릿말/꼬릿말 ----
def _make_on_page(meta):
    logo_path = _asset('ci_logo.jpg')
    has_logo = os.path.exists(logo_path)

    def _on_page(canvas, doc):
        canvas.saveState()
        w, h = A4
        left = 15 * mm
        right = w - 15 * mm
        top = h - 12 * mm
        # --- 머릿말: CI 로고 + 타이틀 + 네이비 밑줄 ---
        if has_logo:
            # 596x140 비율 유지, 높이 8mm
            lh = 8 * mm
            lw = lh * (596.0 / 140.0)
            try:
                canvas.drawImage(logo_path, left, top - lh + 1 * mm, width=lw, height=lh,
                                 mask='auto', preserveAspectRatio=True)
            except Exception:
                pass
        canvas.setFont('NanumBold', 9)
        canvas.setFillColor(NAVY)
        canvas.drawRightString(right, top - 5 * mm, '자산 분석 리포트 · KIWOOM F&I ASSET REPORT')
        canvas.setStrokeColor(NAVY)
        canvas.setLineWidth(1.4)
        line_y = top - 10 * mm
        canvas.line(left, line_y, right, line_y)
        # --- 꼬릿말: 페이지 번호 + 고지문 ---
        canvas.setFont('Nanum', 7)
        canvas.setFillColor(MUTED)
        canvas.setStrokeColor(LINE)
        canvas.setLineWidth(0.5)
        canvas.line(left, 15 * mm, right, 15 * mm)
        canvas.drawString(left, 11 * mm,
                          '본 리포트는 입력 데이터 기반 자동 생성 자료입니다. 실제 거래 시 등기부·매각물건명세서 등 공식자료를 확인하세요.')
        canvas.drawRightString(right, 11 * mm, '- %d -' % doc.page)
        canvas.restoreState()

    return _on_page


# ---- 섹션 → flowable ----
def _kv_table(rows, st):
    data = [[Paragraph(str(k), st['kvK']), Paragraph(str(v), st['kvV'])] for k, v in rows]
    t = Table(data, colWidths=[35 * mm, None])
    t.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('LINEBELOW', (0, 0), (-1, -2), 0.4, LINE),
    ]))
    return t


def _grid_table(headers, rows, st, align=None):
    head = [Paragraph(str(h), st['cellH']) for h in headers]
    body = []
    for r in rows:
        cells = []
        for i, c in enumerate(r):
            style = st['cellR'] if (align and i < len(align) and align[i] == 'r') else st['cell']
            cells.append(Paragraph('' if c is None else str(c), style))
        body.append(cells)
    data = [head] + body
    t = Table(data, repeatRows=1)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), NAVY),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('LINEBELOW', (0, 1), (-1, -1), 0.4, LINE),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F7F8FA')]),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    return t


def _sections_to_flowables(sections, st):
    flow = []
    for sec in sections or []:
        typ = sec.get('type')
        if typ == 'h2':
            flow.append(Paragraph(sec.get('text', ''), st['h2']))
        elif typ == 'text':
            flow.append(Paragraph(sec.get('text', ''), st['body']))
        elif typ == 'kv':
            flow.append(_kv_table(sec.get('rows', []), st))
            flow.append(Spacer(1, 4))
        elif typ == 'table':
            flow.append(_grid_table(sec.get('headers', []), sec.get('rows', []),
                                    st, sec.get('align')))
            flow.append(Spacer(1, 4))
        elif typ == 'spacer':
            flow.append(Spacer(1, sec.get('h', 8)))
    return flow


def build_report_pdf(data):
    """data = {meta:{title,subtitle,date}, sections:[...]} → PDF bytes"""
    _ensure_fonts()
    st = _styles()
    meta = (data or {}).get('meta', {})
    sections = (data or {}).get('sections', [])

    buf = io.BytesIO()
    doc = BaseDocTemplate(
        buf, pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=26 * mm, bottomMargin=20 * mm,
        title=meta.get('title', '자산 분석 리포트'),
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin,
                  doc.width, doc.height, id='body',
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id='main', frames=[frame],
                                       onPage=_make_on_page(meta))])

    story = []
    story.append(Paragraph('자산 분석 리포트 · ASSET REPORT',
                           ParagraphStyle('eb', fontName='NanumBold', fontSize=8,
                                          textColor=PINK, spaceAfter=2)))
    story.append(Paragraph(meta.get('title', ''), st['h1']))
    if meta.get('subtitle'):
        story.append(Paragraph(meta.get('subtitle'), st['sub']))
    story.append(Spacer(1, 4))
    story.extend(_sections_to_flowables(sections, st))

    doc.build(story)
    return buf.getvalue()
