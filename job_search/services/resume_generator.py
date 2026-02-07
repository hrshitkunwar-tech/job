from __future__ import annotations
from typing import Optional

import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path("job_search/templates/resume_templates")
OUTPUT_DIR = Path("job_search/static/generated")


class ResumeGenerator:
    def __init__(self):
        TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        self.jinja_env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))

    def generate_pdf(
        self,
        resume_data: dict,
        template_name: str = "default",
        output_filename: Optional[str] = None,
    ) -> Path:
        """Generate a PDF resume from structured data."""
        if not output_filename:
            output_filename = f"resume_{resume_data.get('name', 'output').replace(' ', '_')}.pdf"

        output_path = OUTPUT_DIR / output_filename

        html_content = self._render_html(resume_data, template_name)

        try:
            self._html_to_pdf(html_content, output_path)
        except Exception as e:
            logger.warning(f"WeasyPrint PDF generation failed: {e}. Saving HTML instead.")
            html_path = output_path.with_suffix(".html")
            html_path.write_text(html_content)
            return html_path

        return output_path

    def _render_html(self, resume_data: dict, template_name: str) -> str:
        try:
            template = self.jinja_env.get_template(f"{template_name}.html")
            return template.render(resume=resume_data)
        except Exception:
            # Fallback: use inline template
            return self._inline_template(resume_data)

    def _html_to_pdf(self, html_content: str, output_path: Path) -> None:
        from weasyprint import HTML
        HTML(string=html_content).write_pdf(str(output_path))

    def _inline_template(self, data: dict) -> str:
        """Fallback HTML template when template file is missing."""
        skills_html = ""
        for skill in data.get("skills", []):
            skills_html += f'<span style="display:inline-block;background:#f0f0f0;padding:2px 8px;margin:2px;border-radius:4px;font-size:13px;">{skill}</span>'

        experience_html = ""
        for exp in data.get("experience", []):
            bullets = "".join(f"<li>{b}</li>" for b in exp.get("bullets", []))
            experience_html += f"""
            <div style="margin-bottom:16px;">
                <h3 style="margin:0;font-size:16px;">{exp.get('title', '')}</h3>
                <p style="margin:2px 0;color:#666;font-size:14px;">
                    {exp.get('company', '')} | {exp.get('start_date', '')} - {exp.get('end_date', '')}
                </p>
                <p style="font-size:14px;">{exp.get('description', '')}</p>
                <ul style="font-size:14px;margin:4px 0;">{bullets}</ul>
            </div>"""

        education_html = ""
        for edu in data.get("education", []):
            education_html += f"""
            <p style="font-size:14px;"><strong>{edu.get('degree', '')}</strong> - {edu.get('school', '')} ({edu.get('year', '')})</p>"""

        return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8">
<style>
    body {{ font-family: 'Helvetica Neue', Arial, sans-serif; margin: 40px; color: #333; line-height: 1.5; }}
    h1 {{ font-size: 24px; margin-bottom: 4px; }}
    h2 {{ font-size: 18px; border-bottom: 2px solid #333; padding-bottom: 4px; margin-top: 24px; }}
    .contact {{ color: #666; font-size: 14px; }}
</style>
</head>
<body>
    <h1>{data.get('name', '')}</h1>
    <p class="contact">
        {data.get('email', '')}
        {(' | ' + data.get('phone', '')) if data.get('phone') else ''}
        {(' | ' + data.get('location', '')) if data.get('location') else ''}
    </p>

    {('<h2>Summary</h2><p style="font-size:14px;">' + data.get('summary', '') + '</p>') if data.get('summary') else ''}

    <h2>Skills</h2>
    <div>{skills_html}</div>

    <h2>Experience</h2>
    {experience_html}

    {('<h2>Education</h2>' + education_html) if data.get('education') else ''}
</body>
</html>"""
