"""Reject unexpected empty rendered lines between project bullets."""
import argparse
import statistics
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path


def check_project_spacing(pdf):
    xml = subprocess.check_output(['pdftotext', '-bbox', str(pdf), '-'], text=True)
    root = ET.fromstring(xml)
    rows = {}
    for word in root.iter():
        if word.tag.rsplit('}', 1)[-1] == 'word':
            y = round(float(word.attrib['yMin']))
            rows.setdefault(y, []).append((float(word.attrib['xMin']), word.text or ''))
    merged = {}
    for y, words in sorted(rows.items()):
        key = next(reversed(merged)) if merged else y
        if y - key > 2:
            key = y
        merged.setdefault(key, []).extend(words)
    rows = merged
    active = False
    previous = None
    in_bullet = False
    continuations = []
    gaps = []
    for y, words in sorted(rows.items()):
        text = ' '.join(word for _, word in sorted(words))
        compact = ''.join(text.split()).casefold()
        if compact == 'projects':
            active = True
            continue
        if not active:
            continue
        if compact in ('achievements', 'education', 'experience', 'technicalskills', 'certifications'):
            break
        # Bullet glyphs can have a slightly different baseline from their text.
        bullet = '•' in text
        if bullet:
            if in_bullet and previous is not None:
                gaps.append(y - previous)
            in_bullet = True
        elif in_bullet and previous is not None:
            delta = y - previous
            if delta > 3:
                continuations.append(delta)
        previous = y
    baseline = statistics.median(continuations) if continuations else 12
    if any(gap > baseline * 1.6 for gap in gaps):
        raise ValueError('Unexpected blank rendered line between project bullets; shorten the preceding bullet and recompile')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pdf', type=Path)
    args = parser.parse_args()
    check_project_spacing(args.pdf)
    print('PASS: project bullet spacing')


if __name__ == '__main__':
    main()
