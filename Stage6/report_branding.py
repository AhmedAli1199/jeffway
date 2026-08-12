"""Shared Jeffway branding for Stage 6 PDF reports."""

from html import escape as _esc

LOGO_URL = "https://jeffway.co.uk/wp-content/uploads/2025/05/jeffway-group-1.svg"
PRIMARY = "#145c63"
PRIMARY_DARK = "#0f4549"
ACCENT = "#2f8f7a"
TEXT = "#17212b"
MUTED = "#56616b"
BORDER = "#dce3e8"
SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1mkklLpvi8klaN4Zo4D_cBrb95PGQ4NbUvtTAvLI4CII/edit"
)

PDF_HEADER_STYLES = """
.jw-header {
  background: linear-gradient(135deg, #145c63 0%, #0f4549 100%);
  color: #fff;
  border-radius: 8px;
  padding: 18px 22px 16px;
  margin-bottom: 18px;
  display: flex;
  align-items: center;
  gap: 18px;
}
.jw-header img {
  max-width: 130px;
  max-height: 44px;
  display: block;
}
.jw-header .jw-title h1 {
  margin: 0 0 4px;
  font-size: 21px;
  font-weight: 700;
}
.jw-header .jw-title p {
  margin: 0;
  opacity: 0.9;
  font-size: 11px;
}
.jw-footer {
  margin-top: 24px;
  padding-top: 10px;
  border-top: 1px solid #eef2f4;
  color: #8a949c;
  font-size: 10px;
  text-align: center;
}
"""


def pdf_header_html(title: str, subtitle: str) -> str:
    return (
        '<div class="jw-header">'
        f'<div class="jw-logo"><img src="{LOGO_URL}" alt="Jeffway Group" /></div>'
        '<div class="jw-title">'
        f"<h1>{_esc(title)}</h1>"
        f"<p>{_esc(subtitle)}</p>"
        "</div>"
        "</div>"
    )


def pdf_footer_html(tagline: str) -> str:
    return f'<div class="jw-footer">Jeffway Group · {_esc(tagline)}</div>'
