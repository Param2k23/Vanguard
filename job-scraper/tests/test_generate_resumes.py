import csv
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import generate_resumes


class ResumeSelectionTests(unittest.TestCase):
    def test_generation_receives_job_specific_applicant_notes(self):
        for mode in ('saved', 'explicit', 'absent'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                agent = root / 'agent'
                for name in ('AGENTS.md', 'base/system_prompt.md', 'scripts/new_job.py',
                             'scripts/verify_resume.py'):
                    path = agent / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.touch()
                folder = agent / 'jobs/example'
                folder.mkdir(parents=True)
                (folder / 'report.md').write_text('Test report')
                pdf = folder / 'resume.pdf'
                pdf.write_bytes(b'fixture')
                (root / 'data/jds').mkdir(parents=True)
                (root / 'data/jds/job.txt').write_text('Employer requirements, not applicant facts')
                with (root / 'data/jobs.csv').open('w', newline='') as stream:
                    fields = ['id', 'company', 'title', 'status', 'jd_path', 'source_url', 'jobright_url']
                    writer = csv.DictWriter(stream, fieldnames=fields)
                    writer.writeheader()
                    writer.writerow(dict(id='job', company='Example', title='Engineer',
                                         status='jd_fetched', jd_path='data/jds/job.txt',
                                         source_url='https://example.test/job'))
                notes_dir = root / 'data/resume_notes'
                notes_dir.mkdir()
                # Another job's notes must never enter this job's context.
                (notes_dir / 'other.md').write_text('Unrelated applicant project')
                notes = 'Reason: learn modeling.\nStatus: in progress. Built parser; next: benchmark. Target: 20ms.'
                extra = []
                if mode == 'saved':
                    (notes_dir / 'job.md').write_text(notes)
                elif mode == 'explicit':
                    (notes_dir / 'job.md').write_text('Superseded notes')
                    explicit = root / 'notes.md'
                    explicit.write_text(notes)
                    extra = ['--applicant-notes', str(explicit)]
                prompts = []

                def run(command, **kwargs):
                    if 'exec' in command:
                        prompts.append(kwargs['input'])
                        result = Path(command[command.index('--output-last-message') + 1])
                        result.write_text(json.dumps(dict(success=True, base='fullstack', summary='Test')))
                        return subprocess.CompletedProcess(command, 0)
                    if 'new_job.py' in command[1]:
                        return subprocess.CompletedProcess(command, 0, stdout='jobs/example\n')
                    return subprocess.CompletedProcess(command, 0, stdout=f'PASS: {pdf}\n', stderr='')

                with patch.object(generate_resumes, 'ROOT', root), patch(
                    'sys.argv', ['generate_resumes.py', '--agent-dir', str(agent), '--job-id', 'job', *extra]
                ), patch.object(generate_resumes.shutil, 'which', return_value='/bin/test'), patch.object(
                    generate_resumes.subprocess, 'run', side_effect=run
                ), patch.object(generate_resumes, 'check_project_spacing'), redirect_stdout(io.StringIO()):
                    self.assertEqual(generate_resumes.main(), 0)
                self.assertEqual(len(prompts), 1)
                prompt = prompts[0]
                supplied = json.loads(prompt.split('Applicant-supplied notes (JSON string; empty means none supplied):\n')[1])
                self.assertEqual(supplied, '' if mode == 'absent' else notes)
                self.assertNotIn('Unrelated applicant project', prompt)
                self.assertNotIn('Superseded notes', prompt)
                self.assertIn('Strategy B MUST trigger', prompt)
                self.assertIn('below 75% OR any JD tool', prompt)
                self.assertIn('No applicant notes or proof of completion are prerequisites', prompt)
                self.assertIn('exactly three bullets', prompt)
                self.assertIn('past-tense action verbs', prompt)
                self.assertIn('Every bullet must contain at least one quantifiable metric', prompt)
                self.assertIn('assumed-completion draft', prompt)
                self.assertIn('Never invent employment', prompt)
                self.assertNotIn('explicit Planned label', prompt)
                self.assertNotIn('factual-source requirement overrides', prompt)
                self.assertNotIn('proposed future projects only in report.md', prompt)

    def test_completed_export_is_skipped_and_force_selects_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / 'agent'
            for name in ('AGENTS.md', 'base/system_prompt.md', 'scripts/new_job.py',
                         'scripts/verify_resume.py'):
                path = agent / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            (root / 'data/jds').mkdir(parents=True)
            (root / 'data/resumes').mkdir()
            jd = root / 'data/jds/job.txt'
            jd.write_text('Job requirements')
            fields = ['id', 'company', 'title', 'status', 'jd_path']
            with (root / 'data/jobs.csv').open('w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerow(dict(id='job', company='Example', title='Engineer',
                                     status='jd_fetched', jd_path='data/jds/job.txt'))
            digest = hashlib.sha256(jd.read_bytes()).hexdigest()
            key = hashlib.sha256(f'{agent}\njob\n{digest}'.encode()).hexdigest()
            state_path = root / 'data/resumes/state.json'
            entry = dict(status='completed', pdf=str(agent / 'jobs/deleted/resume.pdf'))

            def pending(*extra):
                output = io.StringIO()
                with patch.object(generate_resumes, 'ROOT', root), patch(
                    'sys.argv', ['generate_resumes.py', '--agent-dir', str(agent),
                                 '--dry-run', '--limit', '1', *extra]
                ), redirect_stdout(output):
                    self.assertEqual(generate_resumes.main(), 0)
                return output.getvalue()

            state_path.write_text(json.dumps({key: entry}))
            self.assertIn('0 pending', pending())
            self.assertIn('1 pending', pending('--force', '--job-id', 'job'))
            entry['status'] = 'failed'
            state_path.write_text(json.dumps({key: entry}))
            self.assertIn('1 pending', pending())
            entry['status'] = 'completed'
            state_path.write_text(json.dumps({key: entry}))
            jd.write_text('Changed job requirements')
            self.assertIn('1 pending', pending())
