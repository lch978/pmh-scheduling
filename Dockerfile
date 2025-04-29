FROM python:3.9-slim

# Prevent Python from writing .pyc files to disk and enable stdout/stderr logging
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set working directory
WORKDIR /app

# Install system dependencies (if needed)
RUN apt-get update && apt-get install -y gcc

# Copy dependency file and install Python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy the rest of the application code
COPY . /app

# Expose the port (Flask default is 5000)
EXPOSE 5000

# Set environment variable for Flask
ENV FLASK_APP=app.py

# Start the Flask app
CMD ["flask", "run", "--host=0.0.0.0", "--port=5000"]