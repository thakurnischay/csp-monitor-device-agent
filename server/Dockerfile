FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# CSP_MONITOR_SECRET_KEY must be passed in at run time, e.g.:
#   docker run -e CSP_MONITOR_SECRET_KEY="<a real random secret>" ...
# Never hardcode it here - app.py falls back to a dev-only default if unset.
EXPOSE 5100
CMD ["waitress-serve", "--host=0.0.0.0", "--port=5100", "app:app"]
