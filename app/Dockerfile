FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bank ./bank
EXPOSE 8000 8080
CMD ["uvicorn", "bank.coordinator:app", "--host", "0.0.0.0", "--port", "8000"]
