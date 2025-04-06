from flask import Flask, request, jsonify, render_template, redirect, url_for, flash
from flask_pymongo import PyMongo
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from bson.objectid import ObjectId, InvalidId
from werkzeug.security import generate_password_hash, check_password_hash
import requests
import json
import os
import re  # Added for regex pattern matching

app = Flask(__name__)

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# MongoDB Configuration
app.config["MONGO_URI"] = os.getenv("MONGO_URI", "mongodb://localhost:27017/law")
mongo = PyMongo(app)

# Flask-Login Configuration
app.secret_key = os.getenv("SECRET_KEY", "your_secret_key")
login_manager = LoginManager()
login_manager.init_app(app)

class User(UserMixin):
    def __init__(self, user_id, username):
        self.id = user_id
        self.username = username
    def get_id(self):
        return self.id

@login_manager.user_loader
def load_user(user_id):
    user = mongo.db.users.find_one({"_id": ObjectId(user_id)})
    return User(str(user['_id']), user['username']) if user else None

# Gemini API
API_KEY = os.getenv("GEMINI_API_KEY", "AIzaSyBZCrBZMc_eeM-Asa-O869cL1i6LoKb8PA")  # Your provided key
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent?key={API_KEY}"

def get_palm_response(query):
    headers = {'Content-Type': 'application/json'}
    payload = {"contents": [{"parts": [{"text": query}]}]}
    response = requests.post(GEMINI_API_URL, json=payload, headers=headers)
    print("Full API Response:", response.text)
    if response.status_code == 200:
        try:
            json_response = response.json()
            if 'candidates' in json_response:
                content = json_response['candidates'][0].get('content', {})
                parts = content.get('parts', [])
                if parts and 'text' in parts[0]:
                    return parts[0]['text']
            return "No valid response from PaLM API."
        except json.JSONDecodeError:
            return "Error parsing the response JSON."
    else:
        return f"Error: {response.status_code} - {response.text}"

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = mongo.db.users.find_one({"username": username})
        if user and check_password_hash(user['password'], password):
            login_user(User(str(user['_id']), username))
            return redirect(url_for('dashboard'))
        else:
            flash("Invalid credentials.")
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        if mongo.db.users.find_one({"username": username}):
            flash("Username already exists.")
        else:
            hashed_password = generate_password_hash(password)
            mongo.db.users.insert_one({"username": username, "password": hashed_password})
            flash("Registration successful. Please log in.")
            return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/dashboard')
@login_required
def dashboard():
    chats = mongo.db.chats.find({"username": current_user.username})
    return render_template('dashboard.html', username=current_user.username, chats=chats)

@app.route('/new_chat', methods=['POST'])
@login_required
def new_chat():
    chat_id = mongo.db.chats.insert_one({"username": current_user.username, "messages": []}).inserted_id
    return jsonify({'chat_id': str(chat_id)})

@app.route('/get_chat/<chat_id>', methods=['GET'])
@login_required
def get_chat(chat_id):
    try:
        chat = mongo.db.chats.find_one({"_id": ObjectId(chat_id)})
        if chat:
            return jsonify({'messages': chat['messages']})
        return jsonify({'messages': []})
    except InvalidId:
        return jsonify({'error': 'Invalid chat_id format'}), 400

@app.route('/ask', methods=['POST'])
@login_required
def ask_query():
    user_input = request.form.get('query')
    chat_id = request.form.get('chat_id')
    print(f"Received query: {user_input}, chat_id: {chat_id}")

    if not user_input or not chat_id or not isinstance(chat_id, str) or len(chat_id) != 24:
        return jsonify({'error': 'Invalid or missing query or chat_id'}), 400

    try:
        chat = mongo.db.chats.find_one({"_id": ObjectId(chat_id)})
        if not chat:
            return jsonify({'error': 'Chat not found'}), 404

        messages = chat.get('messages', [])
        messages.append({'sender': 'user', 'text': user_input})
        mongo.db.chats.update_one({"_id": ObjectId(chat_id)}, {"$set": {"messages": messages}})

        # Refine search to match full or specific patterns
        local_result = None
        if "section" in user_input.lower() or "ipc" in user_input.lower() or "article" in user_input.lower():
            match = re.search(r"(?:what is )?(?:section|article)\s+(\d+[a-zA-Z]?)", user_input.lower())
            if match:
                section_number = match.group(1)
                local_result = mongo.db.local.find_one({"question": {"$regex": rf"(?:section|article)\s+{section_number}", "$options": "i"}})
        else:
            local_result = mongo.db.local.find_one({"question": {"$regex": user_input, "$options": "i"}})

        print(f"Local search for '{user_input}': {local_result}")
        if local_result:
            law_response = local_result['answer'].strip('"') if local_result['answer'].startswith('"') else local_result['answer']
        else:
            # Updated prompt to handle irrelevant or offensive queries politely
            api_query = (
    f"{user_input}\n\n"
    "If this is a valid legal query under Indian law, provide a detailed legal response citing laws, sections, and possible solutions. "
    "If it is unrelated, unclear, or inappropriate, respond ONLY with: "
    "'I'm here to assist with genuine legal queries under Indian law. Please refine your question accordingly.'"    
    )


            law_response = get_palm_response(api_query)

            # If it's a polite rejection, don't store it
            rejection_text = "I'm here to assist with genuine legal queries under Indian law. Please refine your question accordingly."
            if law_response.strip() != rejection_text:
                mongo.db.local.insert_one({
                    "question": user_input,
                    "answer": law_response
                })

        messages.append({'sender': 'bot', 'text': law_response})
        mongo.db.chats.update_one({"_id": ObjectId(chat_id)}, {"$set": {"messages": messages}})

        return jsonify({'response': law_response})
    except InvalidId:
        return jsonify({'error': 'Invalid chat_id format'}), 400
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        return jsonify({'error': f'An unexpected error occurred: {str(e)}'}), 500

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('home'))

if __name__ == '__main__':
    app.run(debug=True)