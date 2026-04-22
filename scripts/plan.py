"""
Creative Studio — Phase 3: Plan
Takes analysis results and brief answers, recommends a design strategy.
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
import json

API_KEY = os.environ.get("GEMINI_API_KEY", "")
if not API_KEY:
    print("ERROR: GEMINI_API_KEY environment variable required.", file=sys.stderr)
    sys.exit(1)


PLAN_PROMPT_TEMPLATE = """
You are a senior design strategist at a top creative agency. Your job is to recommend the EXACT workflow for executing a design project based on the client's brief and reference image analysis.

## Client Brief Answers
{brief_json}

## Reference Image Analysis
{analysis_json}

## Decision Matrix
Choose ONE recommended approach:

**A. FULL AI GENERATION** — Use when: generic product is fine, text/label doesn't need to be exact, budget is tight.
  Pros: Fastest, cheapest.
  Cons: Can't control exact branding, labels may be gibberish.

**B. AI BACKGROUND + MANUAL COMPOSITE** — Use when: exact product label matters, product is already photographed/cut out, shelf/environment needs to be realistic.
  Pros: Product is 100% real, background can be AI-generated.
  Cons: Requires Photoshop/GIMP compositing step.

**C. MOOD BOARD + PHOTO SHOOT BRIEF** — Use when: this needs to be truly professional, print-ready, or the physical shelf arrangement is complex.
  Pros: Highest quality, full control.
  Cons: Most expensive, requires studio or location shoot.

**D. MULTIPLE AI VARIATIONS + CLIENT REVIEW** — Use when: direction is unclear, client needs to see options before committing.
  Pros: Explores possibilities.
  Cons: Higher token cost.

## Your Output
Respond ONLY as valid JSON in this exact shape:
{{
  "recommended_approach": "A | B | C | D",
  "approach_name": "e.g. AI Background + Manual Composite",
  "confidence": "high | medium | low",
  "rationale": "2-3 sentences explaining why",
  "deliverables": [
    "e.g. Clean shelf background PNG (transparent products removed)",
    "e.g. Product cutout with shadow layer"
  ],
  "tools_needed": ["e.g. Photoshop", "e.g. Imagen 4", "e.g. Nano Banana"],
  "steps": [
    "Step 1: ...",
    "Step 2: ..."
  ],
  "estimated_time": "e.g. 30 min background gen + 15 min composite",
  "risk_factors": ["e.g. Nano Banana may distort product label"],
  "prompts": {{
    "background_prompt": "detailed prompt for generating the environment",
    "product_placement_prompt": "detailed prompt for compositing guidance"
  }}
}}

Be honest. If the analysis shows the reference image has floating products or physically impossible shadows, flag that the AI-generated background must correct those flaws.
"""


def plan_strategy(brief_answers: dict, analysis: dict) -> dict:
    from google import genai

    client = genai.Client(api_key=API_KEY)
    prompt = PLAN_PROMPT_TEMPLATE.format(
        brief_json=json.dumps(brief_answers, indent=2),
        analysis_json=json.dumps(analysis, indent=2)
    )

    try:
        resp = client.models.generate_content(
            model="gemini-3.1-pro-preview",
            contents=prompt,
            config=genai.types.GenerateContentConfig(temperature=0.3)
        )
        text = resp.text.strip() if resp.text else "{}"
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif text.startswith("```"):
            text = text.strip("`").strip()
        return json.loads(text)
    except Exception as e:
        return {"error": str(e), "recommended_approach": "B", "rationale": "Fallback: composite is safest for branded products"}


def format_plan(plan: dict) -> str:
    lines = ["\n── STRATEGY PLAN", "=" * 50]
    lines.append(f"  Approach: {plan.get('approach_name', 'Unknown')}")
    lines.append(f"  Confidence: {plan.get('confidence', '?')}")
    lines.append(f"  Rationale: {plan.get('rationale', 'No rationale')}")
    lines.append(f"  Estimated Time: {plan.get('estimated_time', '?')}")
    lines.append("")

    if plan.get("steps"):
        lines.append("  Steps:")
        for step in plan["steps"]:
            lines.append(f"    • {step}")

    if plan.get("risk_factors"):
        lines.append("")
        lines.append("  ⚠ Risk Factors:")
        for risk in plan["risk_factors"]:
            lines.append(f"    • {risk}")

    lines.append("=" * 50)
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--brief", "-b", required=True, help="Path to JSON brief file")
    parser.add_argument("--analysis", "-a", required=True, help="Path to JSON analysis file")
    args = parser.parse_args()

    with open(args.brief) as f:
        brief = json.load(f)
    with open(args.analysis) as f:
        analysis = json.load(f)

    plan = plan_strategy(brief, analysis)
    print(format_plan(plan))
    # Save plan too
    out_path = Path(args.brief).parent / f"{Path(args.brief).stem}-plan.json"
    with open(out_path, "w") as f:
        json.dump(plan, f, indent=2)
    print(f"\n  Plan saved: {out_path}")
