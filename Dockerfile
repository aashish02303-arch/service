FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY psn_stock_alert.py .
CMD ["python", "-u", "psn_stock_alert.py"]
