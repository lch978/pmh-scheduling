# start from an official Python image
FROM python:3.11-slim

# set working dir
WORKDIR /app

# copy & install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# copy the rest of your code
COPY . .

# tell Cloud Run to listen on port 8080
ENV PORT 8080
EXPOSE 8080

# use gunicorn to serve your Flask app
CMD ["gunicorn", "-b", "0.0.0.0:8080", "app:create_app()"]