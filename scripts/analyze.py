"""
Creative Studio — Phase 2: Analyze
Uses a vision-enabled reasoning model to intelligently describe reference images.
"""
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "google-genai>=1.0.0",
#     "pillow>=10.0.0",
# ]
# ///
import os
import sys
from pathlib import Path
from io import BytesIO

API_KEY = os.environ.get("GEMINI_API_KEY", "")
if not API_KEY:
    print("ERROR: GEMINI_API_KEY environment variable required.", file=sys.stderr)
    sys.exit(1)


def analyze_image(image_path: str, context: str = "") -> dict:
    """
    Send an image to Gemini 2.5 Pro Vision and get a structured analysis.
    Returns: dict with scene_type, lighting, colors, composition, key_elements, etc.
    """
    from google import genai
    from PIL import Image

    img = Image.open(image_path)
    client = genai.Client(api_key=API_KEY)

    analysis_prompt = (
        f"You are a senior art director analyzing a reference image for a design project."
        f"{f' Context: {context}' if context else ''}\n\n"
        f"Analyze this image in extreme detail and respond ONLY as valid JSON in this exact shape:\n"
        f'{{\n'
        f'  "scene_type": "e.g. retail store shelf, e-commerce layout, social media grid",\n'
        f'  "primary_subject": "what is the main focus",\n'
        f'  "shelf_fixture": "e.g. gondola, slatwall, wire rack, floating shelf, or none",\n'
        f'  "lighting_direction": "e.g. warm overhead fluoro, front softbox, natural window",\n'
        f'  "lighting_quality": "e.g. harsh shadows, soft diffused, dramatic chiaroscuro",\n'
        f'  "color_temperature": "e.g. warm 3200K, cool 5600K, neutral",\n'
        f'  "dominant_colors": ["#hex1", "#hex2"],\n'
        f'  "background_type": "e.g. clean white, busy store interior, gradient",\n'
        f'  "depth_of_field": "e.g. shallow bokeh, deep focus",\n'
        f'  "camera_angle": "e.g. eye-level, 15° above, straight-on",\n'
        f'  "surrounding_products_count": 0,\n'
        f'  "surrounding_products_description": "e.g. competitor energy drinks, same-brand variants",\n'
        f'  "label_readability": "e.g. all text sharp, some text blurred",\n'
        f'  "physical_plausibility": "e.g. solid contact shadows, or floating",\n'
        f'  "overall_mood": "e.g. premium retail, casual lifestyle, clinical",\n'
        f'  "recommended_approach": "e.g. generate full scene, generate background-only then composite, needs photo shoot"\n'
        f'}}\n\n'
        f'Be brutally honest about physical flaws (floating objects, bad shadows, AI artifacts).'
    )

    try:
        resp = client.models.generate_content(
            model="gemini-3.1-pro-preview",
            contents=[img, analysis_prompt],
            config=genai.types.GenerateContentConfig(temperature=0.2)
        )
        text = resp.text.strip() if resp.text else "{}"
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif text.startswith("```"):
            text = text.strip("`").strip()

        import json
        result = json.loads(text)
        return result
    except Exception as e:
        return {"error": str(e), "status": "failed"}


def format_analysis(analysis: dict) -> str:
    """Pretty-print analysis results."""
    lines = ["\n── ANALYSIS REPORT", "─" * 40]
    for key, val in analysis.items():
        lines.append(f"  {key.replace('_', ' ').title()}: {val}")
    lines.append("─" * 40)
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", "-i", required=True, help="Image path")
    parser.add_argument("--context", "-c", default="", help="Design context")
    args = parser.parse_args()

    result = analyze_image(args.input, args.context)
    print(format_analysis(result))
