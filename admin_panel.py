from flask import Flask, render_template, request, redirect, url_for, session, flash
import sqlite3
import hashlib
from datetime import datetime, timedelta

app = Flask(__name__)
app.secret_key = 'your_secret_key_here'  # Замени на случайный ключ

# Настройки админа
ADMIN_LOGIN = 'admin'
ADMIN_PASSWORD = hashlib.sha256('Danila1032'.encode()).hexdigest()  # Замени 'your_password_here'

# Подключение к SQLite
def get_db_connection():
    conn = sqlite3.connect('trainer_bot.db')
    conn.row_factory = sqlite3.Row
    return conn

@app.route('/')
def index():
    if 'logged_in' not in session:
        return redirect(url_for('login'))
    return redirect(url_for('dashboard'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = hashlib.sha256(request.form['password'].encode()).hexdigest()
        if username == ADMIN_LOGIN and password == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('dashboard'))
        else:
            flash('Неверный логин или пароль')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@app.route('/dashboard')
def dashboard():
    if 'logged_in' not in session:
        return redirect(url_for('login'))
    conn = get_db_connection()
    users = conn.execute('SELECT * FROM users').fetchall()
    subs = conn.execute('SELECT * FROM subscriptions').fetchall()
    promos = conn.execute('SELECT * FROM promocodes').fetchall()
    conn.close()
    return render_template('dashboard.html', users=users, subs=subs, promos=promos)

@app.route('/give_sub', methods=['POST'])
def give_sub():
    if 'logged_in' not in session:
        return redirect(url_for('login'))
    user_id = request.form['user_id']
    days = int(request.form['days'])
    expires_at = (datetime.now() + timedelta(days=days)).isoformat()
    conn = get_db_connection()
    conn.execute('INSERT OR REPLACE INTO subscriptions (user_id, expires_at) VALUES (?, ?)', (user_id, expires_at))
    conn.commit()
    conn.close()
    flash(f'Подписка выдана пользователю {user_id} на {days} дней')
    return redirect(url_for('dashboard'))

if __name__ == '__main__':
    app.run(debug=True, port=5000)