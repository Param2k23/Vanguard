"""Export verified resumes, then clean only inside jobs/. Default: dry run."""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from common import ROOT, read_csv, write_csv, slug, RESUME_PREFIX

BUILD_SUFFIXES = ('.aux', '.log', '.out', '.fls', '.fdb_latexmk', '.synctex.gz')

def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def matches(source, destination):
    return destination.is_file() and source.stat().st_size == destination.stat().st_size and digest(source) == digest(destination)

def windows_path(path):
    parts = path.parts
    if len(parts) >= 3 and parts[1] == 'mnt' and len(parts[2]) == 1:
        return parts[2].upper() + ':\\' + '\\'.join(parts[3:])
    return str(path)

def export(root, base, apply=False, purge=False, mount=None, today=None, skip_unverified=False):
    # Check before even creating destination folders or modifying tracker data.
    if mount is not None:
        if not mount.is_dir() or not os.access(mount, os.W_OK):
            raise ValueError(f'Export drive is not mounted or writable at {mount}; nothing copied or deleted')
        if str(mount).startswith('/mnt/') and not os.path.ismount(mount):
            raise ValueError(f'Export drive is not mounted at {mount}; nothing copied or deleted')
    base = base.resolve()
    if mount is not None and not base.is_relative_to(mount.resolve()):
        raise ValueError('Export base must be inside the requested mount')
    day = base / (today or datetime.date.today()).isoformat()
    jobs = root / 'jobs'
    if jobs.is_symlink():
        raise ValueError('jobs/ must not be a symlink')
    tracker = root / 'tracker/jobs.csv'
    count = deleted = freed = 0
    skipped = []
    failures = 0
    reserved = set(day.iterdir()) if day.exists() else set()
    for job in sorted(jobs.iterdir()) if jobs.exists() else []:
        if not job.is_dir() or job.is_symlink():
            continue
        if (job / '.exported.json').exists():
            skipped.append((job.name, 'already exported'))
            continue
        if skip_unverified and not (job / '.verified.json').is_file():
            skipped.append((job.name, 'unverified draft; left untouched'))
            continue
        try:
            meta = json.loads((job / 'job.json').read_text())
            if not (job / '.verified.json').is_file():
                raise ValueError('no verification marker; run verify_resume.py --all')
            receipt = json.loads((job / '.verified.json').read_text())
            pdf = job / receipt['pdf']
            if pdf.parent != job or pdf.suffix != '.pdf':
                raise ValueError('invalid verified PDF path')
            sources = [pdf, job/'jd.txt', job/'report.md']
            if any(p.is_symlink() or not p.is_file() for p in sources + [job/'resume.tex']):
                raise ValueError('missing file or symlink source')
            if digest(pdf) != receipt['pdf_sha256'] or digest(job/'resume.tex') != receipt['tex_sha256']:
                (job/'.verified.json').unlink(missing_ok=True)
                raise ValueError('verification marker is stale (source/PDF hash changed); run verify_resume.py --all')
            name = RESUME_PREFIX + '_' + re.sub(r'[^A-Za-z0-9_-]+', '_', meta['company']).strip('_')
            dest = day / (name + '.pdf')
            if dest in reserved:
                dest = day / (name + '_' + slug(meta['role']) + '.pdf')
            index = 2
            stem = dest.stem
            while dest in reserved:
                dest = day / f'{stem}_{index}.pdf'; index += 1
            targets = [dest, day/job.name/'jd.txt', day/job.name/'report.md']
            if any(p.exists() for p in targets[1:]):
                raise ValueError('job review destination already exists')
            rows = read_csv(tracker)
            matching = [r for r in rows if all(r.get(k, '').strip().casefold() == meta[k].strip().casefold() for k in ('company','role'))]
            if purge and len(matching) != 1:
                raise ValueError('purge requires exactly one matching tracker row')
            # Do not follow directory symlinks or remove unrelated nested files.
            cleanup = [pdf] + [p for p in job.iterdir() if p.is_file() and not p.is_symlink() and p.name.endswith(BUILD_SUFFIXES)]
            for build in job.glob('.resume-build-*'):
                if build.is_dir() and not build.is_symlink():
                    cleanup += [p for p in build.rglob('*') if p.is_file() and not p.is_symlink() and not any(x.is_symlink() for x in p.parents) and p.name.endswith(BUILD_SUFFIXES)]
            for src, dst in zip(sources, targets):
                print(f'COPY {src} -> {dst}')
            if purge:
                for path in sorted(job.rglob('*')):
                    print(f'DELETE {path} (purge, after verification and tracker update)')
                print(f'DELETE DIRECTORY {job} (after verification and tracker update)')
            else:
                for path in cleanup: print(f'DELETE {path} (after verification)')
            reserved.add(dest)
            if not apply:
                continue
            for src, dst in zip(sources, targets):
                dst.parent.mkdir(parents=True, exist_ok=True)
                # Exclusive creation prevents accidental overwrite after planning.
                with src.open('rb') as inp, dst.open('xb') as out:
                    shutil.copyfileobj(inp, out)
            if not all(matches(src, dst) for src, dst in zip(sources, targets)):
                raise ValueError('export size/SHA-256 verification failed; no cleanup')
            timestamp = datetime.datetime.now().astimezone().isoformat()
            for row in matching:
                row.update(export_path=windows_path(dest), exported_at=timestamp)
            if matching:
                fields = list(rows[0])
                for field in ('export_path','exported_at'):
                    if field not in fields: fields.append(field)
                write_csv(tracker, fields, rows)
            (job/'.exported.json').write_text(json.dumps({'exported_at':timestamp, 'export_path':windows_path(dest)}))
            if purge:
                # shutil.rmtree unlinks symlinks without traversing their targets.
                cleanup = [p for p in job.rglob('*') if p.is_file() and not p.is_symlink() and not any(x.is_symlink() for x in p.parents)]
                freed += sum(p.stat().st_size for p in cleanup)
                deleted += len(cleanup)
                shutil.rmtree(job)
            else:
                for path in cleanup:
                    size = path.stat().st_size
                    path.unlink(); deleted += 1; freed += size
            count += 1
        except (OSError, ValueError, KeyError) as exc:
            skipped.append((job.name, str(exc)))
            failures += 1
    print(f'{"APPLY" if apply else "DRY RUN"}: jobs exported={count}; day folder={windows_path(day)}; files deleted={deleted}; bytes freed in WSL={freed}')
    for name, reason in skipped: print(f'SKIP {name}: {reason}')
    return int(failures > 0)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--dry-run', action='store_true')
    parser.add_argument('--root', type=Path, default=ROOT, help='Resume workflow directory')
    parser.add_argument('--mount', type=Path, help='Require this export drive to be mounted and writable')
    parser.add_argument('--skip-unverified', action='store_true',
                        help='Leave drafts without verification receipts untouched without failing the batch')
    parser.add_argument('--purge-job', action='store_true')
    args = parser.parse_args()
    base = os.environ.get('RESUME_EXPORT_DIR')
    if not base:
        parser.error('Set RESUME_EXPORT_DIR to your export directory')
    try:
        return export(args.root.resolve(), Path(base), args.apply, args.purge_job,
                      mount=args.mount, skip_unverified=args.skip_unverified)
    except (OSError, ValueError) as exc:
        print(f'STOP: {exc}'); return 1

if __name__ == '__main__':
    raise SystemExit(main())
