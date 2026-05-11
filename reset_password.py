from werkzeug.security import generate_password_hash
import sqlite3

# Connect to database
conn = sqlite3.connect('dental.db')
cursor = conn.cursor()

# New password (change this to what you want)
new_password = "admin123"  # Change this to your desired password

# Hash the password
hashed_password = generate_password_hash(new_password)

# Update admin password
cursor.execute(
    "UPDATE users SET password = ? WHERE username = 'admin'",
    (hashed_password,)
)
conn.commit()

print(f"✅ Admin password has been reset to: {new_password}")

conn.close()