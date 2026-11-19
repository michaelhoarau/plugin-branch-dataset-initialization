"""Generate escaped, styled HTML fragments for the Dataiku macro report."""
from html import escape
from difflib import SequenceMatcher
import re


def table_start(headers, label):
    """Open a styled table, including its scroll wrapper, header, and body.

    Args:
        headers (iterable[str]): Column titles, escaped before insertion.
        label (str): Accessible table label, escaped for use in an attribute.

    Returns:
        str: Opening HTML to combine with rows and ``table_end()``.
    """
    html = ('<div style="overflow-x:auto;border:1px solid #dce3e9;border-radius:6px;margin:12px 0 24px;">'
            '<table aria-label="{}" style="width:100%;border-collapse:collapse;font-size:12px;line-height:1.5;'
            'color:#253444;text-align:left;background:#fff;"><thead><tr>').format(escape(label, quote=True))

    for header in headers:
        html += ('<th scope="col" style="padding:6px 16px;background:#f1f5f8;'
                 'border-bottom:1px solid #dce3e9;text-align:left;font-weight:600;'
                 'white-space:nowrap;">{}</th>').format(escape(header))

    return html + '</tr></thead><tbody>'


def table_row(values, index, current=False, monospace=(), numeric=(), emphasized=(), muted=False, tone=None, shared_location_column=None, nowrap=(), word_wrap=(), emphasis_color='#1672b8'):
    """Render one escaped data row with optional cell and row styling.

    Args:
        values (iterable): Cell values converted to strings and HTML-escaped. False values display an em dash.
        index (int): Zero-based logical row index controlling alternating colors.
        current (bool): Highlight the row and badge its first cell as current.
        monospace (tuple[int]): Zero-based columns using a monospace font.
        numeric (tuple[int]): Zero-based columns using right-aligned numbers.
        emphasized (tuple[int]): Zero-based columns shown in bold.
        emphasis_color (str): Text color for emphasized cells, blue by default.
        muted (bool): Override row colors with grey, including emphasized cells.
        tone (str or None): Optional success, warning, or error background for
            this row's cells only, with a matching status dot in the first cell.
            Muted styling takes precedence and suppresses the dot.
        shared_location_column (int or None): Cell index beneath whose escaped
            value the shared-location warning should appear.
        nowrap (tuple[int]): Zero-based columns whose contents stay on one line.
        word_wrap (tuple[int]): Zero-based columns that wrap at word boundaries,
            instead of allowing arbitrary breaks inside words.

    Returns:
        str: A complete HTML table row. No external state is modified.
    """
    background = '#eaf5fc' if current else ('#f8fafc' if index % 2 else '#ffffff')
    colors = {'success': '#e8f5e9', 'warning': '#fff3e0', 'error': '#ffebee'}
    if tone in colors:
        background = colors[tone]
    if muted:
        background = '#f3f4f6'

    html = '<tr style="background:{};">'.format(background)
    for column, value in enumerate(values):
        style = 'padding:6px 16px;border-bottom:1px solid #e8edf1;vertical-align:top;overflow-wrap:anywhere;'
        if column in nowrap:
            style += 'white-space:nowrap;overflow-wrap:normal;word-break:normal;'
        elif column in word_wrap:
            style += 'white-space:normal;overflow-wrap:normal;word-break:normal;'
        if tone in colors:
            style += 'background-color:{};'.format(background)
        if muted:
            style += 'color:#707780;'

        if column in monospace:
            style += 'font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:11px;'

        if column in numeric:
            style += 'text-align:right;font-variant-numeric:tabular-nums;'

        text = escape(str(value)) if value else '<span style="color:#8995a3;">—</span>'
        if column == 0 and tone in colors and not muted:
            dot_colors = {
                'success': ('#a5d6a7', '#2e7d32', '#1b5e20'),
                'warning': ('#ffd180', '#e07800', '#a64b00'),
                'error': ('#ffabab', '#c62828', '#8e1717'),
            }
            labels = {'success': 'Success', 'warning': 'Warning', 'error': 'Error'}
            highlight, color, edge = dot_colors[tone]
            text += (
                '<span role="img" aria-label="{}" title="{}" '
                'style="display:inline-block;width:8px;height:8px;border-radius:50%;'
                'background-color:{};background-image:radial-gradient(circle at 30% 25%,#ffffff 0%,{} 22%,{} 65%,{} 100%);'
                'border:1px solid {};box-shadow:inset 0 1px 1px rgba(255,255,255,0.6),0 1px 2px rgba(0,0,0,0.3);'
                'margin-left:7px;vertical-align:middle;"></span>'
            ).format(labels[tone], labels[tone], color, highlight, color, edge, edge)
        if column in emphasized:
            color = '#707780' if muted else emphasis_color
            text = '<strong style="color:{};font-weight:700;">{}</strong>'.format(color, text)

        if current and column == 0:
            text += (' <span style="display:inline-block;margin:4px 0 0 8px;padding:2px 7px;'
                     'border-radius:4px;background:#d0e9f9;color:#145b88;font:600 10px/1.5 Arial,sans-serif;'
                     'white-space:nowrap;">Current project</span>')

        if column == shared_location_column:
            text += '<div style="margin-top:4px;font:11px/1.6 Arial,sans-serif;">{}</div>'.format(shared_location_warning())
        html += '<td style="{}">{}</td>'.format(style, text)

    return html + '</tr>'


def table_end():
    """Return closing body, table, and wrapper tags for ``table_start()``."""

    return '</tbody></table></div>'


def shared_location_warning():
    """Return a red overlap icon, tooltip, and same-location explanation.

    Returns:
        str: Trusted HTML suitable for a Details cell or location comparison row.
    """
    warning = (
        "Source and target datasets point to the same location. Their data overlaps: "
        "running recipes or writing data in the duplicated project can overwrite "
        "or replace data used by the source project."
    )
    return (
        '<span role="img" tabindex="0" aria-label="{}" title="{}" '
        'style="color:#c62828;font-size:17px;cursor:help;margin-right:8px;">&#9888;&#65038;</span>'
        '<span style="color:#536273;">Same location, nothing to change.</span>'
    ).format(escape("Warning: " + warning, quote=True), escape(warning, quote=True))


def location_row(source, target, index, muted=False, columns=4):
    """Render a location comparison or a shared-location warning row.

    Args:
        source (str or None): Resolved location in the selected source project.
        target (str or None): Resolved location in the current branch project.
        index (int): Logical dataset row index used to match its background.
        muted (bool): Apply grey styling instead of colored difference highlights.
        columns (int): Number of table columns spanned by the comparison cell.

    Returns:
        str: Escaped HTML with common text dimmed and differences emphasized.
        Identical nonempty strings produce an overlap warning with a tooltip;
        missing locations display Unavailable without implying overlap.

    This is a textual comparison only; it does not query storage or verify
    whether different identifiers resolve to the same physical object.
    """
    background = '#f3f4f6' if muted else ('#f8fafc' if index % 2 else '#ffffff')
    if source and source == target:
        return (
            '<tr style="background:{};"><td colspan="{}" style="padding:4px 16px 10px;'
            'border-bottom:1px solid #e8edf1;font:11px/1.6 Arial,sans-serif;">'
            '{}</td></tr>'
        ).format(background, columns, shared_location_warning())

    def span(text, common, target_side=False):
        """Return an escaped text span, dimmed when common or bold otherwise.

        Empty text produces an empty string; colors come from the enclosing row.
        """
        if not text:
            return ''

        if common:
            return '<span style="color:#8995a3;">{}</span>'.format(escape(text))

        color = '#59616b' if muted else ('#145b88' if target_side else '#854d0e')
        fill = '#e5e7eb' if muted else ('#dceefb' if target_side else '#fef3c7')
        return ('<strong style="color:{};background-color:{};border-radius:2px;'
                'box-decoration-break:clone;-webkit-box-decoration-break:clone;'
                'font-weight:700;">{}</strong>').format(color, fill, escape(text))

    if source is not None and target is not None:
        # Compare complete path components / SQL identifiers. Character matching
        # can misleadingly match fragments inside renamed project identifiers.
        source_parts = re.split(r'([/\\.:])', source)
        target_parts = re.split(r'([/\\.:])', target)
        left, right = [], []
        for operation, i, j, k, l in SequenceMatcher(
                None, source_parts, target_parts, autojunk=False).get_opcodes():
            left.append(span(''.join(source_parts[i:j]), operation == 'equal'))
            right.append(span(''.join(target_parts[k:l]), operation == 'equal', target_side=True))

        source_html, target_html = ''.join(left), ''.join(right)

    else:
        source_html = span(source, False) if source is not None else span('Unavailable', True)
        target_html = span(target, False, target_side=True) if target is not None else span('Unavailable', True)

    return (('<tr style="background:{};"><td colspan="{}" style="padding:4px 16px 10px;'
             'border-bottom:1px solid #e8edf1;">'
             '<div style="display:grid;grid-template-columns:max-content minmax(0,1fr);'
             'column-gap:12px;row-gap:4px;align-items:baseline;">'
             '<span style="font:10px/1.6 Arial,sans-serif;color:#707780;">Source location</span>'
             '<div style="min-width:0;overflow-wrap:anywhere;'
             'font:11px/1.6 ui-monospace,SFMono-Regular,Consolas,monospace;">{}</div>'
             '<span style="font:10px/1.6 Arial,sans-serif;color:#707780;">Target location</span>'
             '<div style="min-width:0;overflow-wrap:anywhere;'
             'font:11px/1.6 ui-monospace,SFMono-Regular,Consolas,monospace;">{}</div>'
             '</div></td></tr>').format(background, columns, source_html, target_html))


def wrap_report(body):
    """Wrap a prepared HTML body in the report container and scoped CSS.

    Args:
        body (str): Trusted HTML fragments with dynamic values already escaped.

    Returns:
        str: Complete report fragment suitable for the macro's HTML result.
        The body is preserved as HTML, rather than escaped again.
    """
    # Every CSS selector is limited to this report to avoid changing the DSS UI.
    return ('''<style>
.bti-macro-report h2 {font-size:18px;font-weight:600;line-height:1.4;margin:24px 0 8px;color:#253444;}
.bti-macro-report p {font-size:13px;line-height:1.6;margin:6px 0 12px;color:#536273;}
</style><div class="bti-macro-report" style="font-family:Arial,sans-serif;padding:4px 8px;max-width:1200px;">'''
            + body + '</div>')
