import unittest
from unittest.mock import patch

from job_pipeline.resume_layout import check_project_spacing


class ProjectSpacingTests(unittest.TestCase):
    def pdf_words(self, third_y):
        words = [('Projects', 10), ('Heading', 20), ('•', 30.9), ('First', 30),
                 ('continuation', 42), ('•', 55), ('Second', 54),
                 ('continuation', 66), ('•', third_y + 1), ('Third', third_y),
                 ('continuation', third_y + 12), ('Achievements', third_y + 30)]
        return '<doc>' + ''.join(
            f'<word xMin="10" yMin="{y}">{text}</word>' for text, y in words
        ) + '</doc>'

    def test_rejects_empty_rendered_line_before_third_bullet(self):
        with patch('job_pipeline.resume_layout.subprocess.check_output', return_value=self.pdf_words(90)):
            with self.assertRaisesRegex(ValueError, 'blank rendered line'):
                check_project_spacing('resume.pdf')

    def test_accepts_even_spacing_with_offset_bullet_glyphs(self):
        with patch('job_pipeline.resume_layout.subprocess.check_output', return_value=self.pdf_words(78)):
            check_project_spacing('resume.pdf')
