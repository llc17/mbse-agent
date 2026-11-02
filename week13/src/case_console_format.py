"""Small, lossless plain-text helpers for the Chinese terminal plan view."""
from decimal import Decimal, InvalidOperation, localcontext
import shutil
import unicodedata


PARAMETER_LABELS = dict(m='总质量', I_pitch='俯仰转动惯量',
                        T_main_max='主轮等效扭矩上限', T_nose_max='鼻轮等效扭矩上限',
                        stepH='楼梯高度', stepD='楼梯深度')
PARAMETER_UNITS = dict(m='kg', I_pitch='kg·m²', T_main_max='N·m', T_nose_max='N·m')


def display_width(text):
    return sum(0 if unicodedata.combining(char) else
               2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
               for char in text)


def terminal_columns():
    return max(1, shutil.get_terminal_size(fallback=(100, 24)).columns)


def wrapped_lines(text, columns, *, indent='', continuation='  '):
    """Insert only newlines/indentation; never shorten or summarize text.

    Whitespace and Chinese punctuation are preferred line boundaries. A long
    token (including a digest) can still wrap safely in a narrow CMD window.
    """
    columns = max(1, columns)
    for paragraph in str(text).split('\n'):
        remaining = paragraph
        prefix = indent
        while remaining:
            # Leave room for content even in an unusually narrow terminal.
            prefix = prefix[:max(0, columns - 2)]
            available = columns - display_width(prefix)
            used, end, boundary = 0, 0, 0
            for char in remaining:
                width = display_width(char)
                if used + width > available and end:
                    break
                used += width
                end += 1
                if char.isspace() or char in '，。；、：！？,;:!?':
                    boundary = end
                if used >= available:
                    break
            if end < len(remaining) and boundary:
                end = boundary
            yield prefix + remaining[:end]
            remaining = remaining[end:]
            prefix = continuation
        if not paragraph:
            yield indent


def parameter_label(name):
    label = PARAMETER_LABELS.get(name)
    return label + '（' + name + '）' if label else str(name)


def parameter_value(name, value):
    if value is None:
        return '未知'
    if name in ('stepH', 'stepD') and not isinstance(value, bool):
        try:
            metres = Decimal(str(value))
            if metres.is_finite():
                # Decimal avoids binary-float artifacts and preserves digits;
                # display conversion never rounds to an integer millimetre.
                with localcontext() as context:
                    context.prec = max(28, len(metres.as_tuple().digits) + 3)
                    millimetres = format(metres * 1000, 'f')
                if '.' in millimetres:
                    millimetres = millimetres.rstrip('0').rstrip('.')
                return millimetres + ' mm（' + str(value) + ' m）'
        except (InvalidOperation, ValueError):
            pass
    unit = PARAMETER_UNITS.get(name, 'm' if name in ('stepH', 'stepD') else '')
    return str(value) + (' ' + unit if unit else '')
