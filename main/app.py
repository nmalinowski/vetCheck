from functools import lru_cache
import json
import os
import logging
import re
from flask import Flask, request, jsonify, send_from_directory
from tenacity import retry, wait_exponential, stop_after_attempt
from openai import OpenAI
from openai import OpenAIError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
OPENROUTER_MODEL = "meta-llama/llama-3.3-8b-instruct:free"

app = Flask(__name__, static_folder='.', static_url_path='')

# Initialize OpenAI client for OpenRouter
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY
)

@app.after_request
def add_noindex_header(response):
    response.headers['X-Robots-Tag'] = 'noindex'
    return response 

def create_prompt(user_data):
    prompt = """You are an expert veterinary diagnostic AI. Provide a JSON response with ranked possible diagnoses for this pet, their likelihood (%), explanation, urgency (true/false), veterinary consultation advice, and home care suggestions based on this data:
"""
    for key, value in user_data.items():
        prompt += f"{key.capitalize()}: {value}\n"
    prompt += """
Return a valid JSON object with fields: conditions (list of {name, likelihood, explanation}), urgent (bool), consult (str), homecare (str). Focus on common conditions for the specified species and breed. Ensure the response is strictly JSON, with no additional text or code blocks.
"""
    return prompt

@retry(wait=wait_exponential(multiplier=1, min=2, max=5), stop=stop_after_attempt(2))
def query_openrouter(prompt):
    try:
        response = client.chat.completions.create(
            model=OPENROUTER_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.15
        )
        return response
    except OpenAIError as e:
        if getattr(e, 'status_code', None) == 503:
            logger.warning(f"OpenRouter 503 error: {str(e)}")
            raise Exception("Model temporarily unavailable. Please try again later.")
        logger.error(f"OpenRouter API error: {str(e)}")
        raise

def process_response(response):
    content = response.choices[0].message.content
    logger.info(f"Raw OpenRouter response: {content}")
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        logger.error(f"Failed to parse JSON response: {content}")
        # Attempt to extract JSON from code blocks or text
        json_match = re.search(r'\{[\s\S]*\}', content)
        if json_match:
            try:
                return json.loads(json_match.group(0))
            except json.JSONDecodeError:
                logger.error("Failed to parse extracted JSON")
        return None

def get_diagnoses(response):
    if response and "conditions" in response:
        return [{"name": c["name"], "likelihood": c["likelihood"], "explanation": c.get("explanation", "")} for c in response["conditions"]]
    return []

def get_highest_ranked_diagnosis(results):
    if not results:
        return "No diagnosis available"
    top_diagnosis = max(results, key=lambda x: x["likelihood"])
    if len(results) >= 3:
        top_three = sorted(results, key=lambda x: x["likelihood"], reverse=True)[:3]
        diagnoses_str = ', '.join(f"{d['name']} ({d['likelihood']}%)" for d in top_three)
        return f"Top 3 possible diagnoses: {diagnoses_str}"
    return f"{top_diagnosis['name']} ({top_diagnosis['likelihood']}%)"

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/scripts.js')
def serve_scripts():
    return send_from_directory('.', 'scripts.js')

@app.route('/styles.css')
def serve_css():
    return send_from_directory('.', 'styles.css')

@app.route('/images/<path:filename>')
def serve_image(filename):
    return send_from_directory('images', filename)

@app.route('/diagnose', methods=['POST'])
def diagnose():
    try:
        user_data = request.json
        prompt = create_prompt(user_data)
        results = []
        queried_models = []
        skipped_models = []
        
        if OPENROUTER_API_KEY:
            openrouter_response = process_response(query_openrouter(prompt))
            if openrouter_response:
                openrouter_diagnoses = get_diagnoses(openrouter_response)
                results.extend(openrouter_diagnoses)
                queried_models.append("OpenRouter")
            else:
                skipped_models.append("OpenRouter (API error)")
        else:
            skipped_models.append("OpenRouter (no API key)")
        
        if not queried_models:
            return jsonify({"error": "No AI models were available to process your request. Please check API configurations.", "skipped_models": skipped_models}), 503
        
        if not results:
            return jsonify({"error": "Failed to parse AI response. Please try again."}), 500

        final_diagnosis = get_highest_ranked_diagnosis(results)
        response = {
            "diagnosis": final_diagnosis,
            "conditions": openrouter_response["conditions"],
            "urgent": openrouter_response["urgent"],
            "consult": openrouter_response["consult"],
            "homecare": openrouter_response["homecare"],
            "disclaimer": "This is not veterinary advice. Please consult a licensed veterinarian for accurate diagnosis and treatment.",
            "queried_models": queried_models,
            "skipped_models": skipped_models
        }
        return jsonify(response)
    except Exception as e:
        logger.error(f"Error in diagnose endpoint: {str(e)}")
        if "Model temporarily unavailable" in str(e):
            return jsonify({"error": "The AI model is currently unavailable due to high demand. Please try again later."}), 503
        return jsonify({"error": "An unexpected error occurred. Please try again."}), 500

@lru_cache(maxsize=128)
@retry(wait=wait_exponential(multiplier=1, min=2, max=5), stop=stop_after_attempt(2))
def query_veterinary_details(diagnosis, species, breed):
    prompt = f"""Provide a JSON object with detailed veterinary information about "{diagnosis}" in {species} (breed: {breed}) using general veterinary knowledge. Include these fields:
    - Overview (str)
    - Symptoms (list or str)
    - When to see a veterinarian (str)
    - Causes (str)
    - Risk factors (list or str)
    - Complications (str)
    - Prevention (str)
    - Treatment options (str)
    Focus on species-specific and breed-specific considerations where relevant.
    Return a valid JSON object, with no additional text or code blocks."""
    try:
        response = client.chat.completions.create(
            model=OPENROUTER_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.15
        )
        content = response.choices[0].message.content
        logger.info(f"Raw OpenRouter veterinary details response: {content}")
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            logger.error(f"Failed to parse JSON response: {content}")
            # Attempt to extract JSON from code blocks or text
            json_match = re.search(r'\{[\s\S]*\}', content)
            if json_match:
                try:
                    return json.loads(json_match.group(0))
                except json.JSONDecodeError:
                    logger.error("Failed to parse extracted JSON")
            return None
    except OpenAIError as e:
        if getattr(e, 'status_code', None) == 503:
            logger.warning(f"OpenRouter 503 error in veterinary details: {str(e)}")
            raise Exception("Model temporarily unavailable. Please try again later.")
        logger.error(f"OpenRouter API error in veterinary details: {str(e)}")
        raise

@app.route('/veterinary-details', methods=['POST'])
def veterinary_details():
    try:
        data = request.json
        diagnosis = data.get("diagnosis")
        species = data.get("species", "Unknown")
        breed = data.get("breed", "Mixed")
        
        if not diagnosis:
            return jsonify({"error": "Diagnosis is required"}), 400
        
        queried_models = []
        skipped_models = []
        
        if OPENROUTER_API_KEY:
            details = query_veterinary_details(diagnosis, species, breed)
            queried_models.append("OpenRouter")
        else:
            details = None
            skipped_models.append("OpenRouter (no API key)")
        
        if not queried_models:
            return jsonify({"error": "No AI models available", "skipped_models": skipped_models}), 503
        
        if not details:
            return jsonify({"error": "Failed to parse AI response for veterinary details. Please try again.", "skipped_models": skipped_models}), 500
        
        response = {
            "diagnosis": diagnosis,
            "species": species,
            "breed": breed,
            "veterinary_details": details if details else "No details available",
            "queried_models": queried_models,
            "skipped_models": skipped_models
        }
        return jsonify(response)
    except Exception as e:
        logger.error(f"Error in veterinary-details endpoint: {str(e)}")
        if "Model temporarily unavailable" in str(e):
            return jsonify({"error": "The AI model is currently unavailable due to high demand. Please try again later."}), 503
        return jsonify({"error": "An unexpected error occurred. Please try again."}), 500

@app.route('/health')
def health():
    missing_keys = []
    available_models = []
    
    if not OPENROUTER_API_KEY:
        missing_keys.append("OPENROUTER_API_KEY")
    else:
        available_models.append("OPENROUTER_API_KEY")
    
    if missing_keys:
        return jsonify({
            "status": "error",
            "message": "Service cannot function - no API key configured",
            "missing_keys": missing_keys
        }), 503
    
    return jsonify({
        "status": "healthy",
        "message": "Service is up and running with OpenRouter API key configured",
        "available_models": available_models
    })

if __name__ == '__main__':
    app.run(debug=True, port=5000)