FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
COPY tests ./tests
RUN python -m py_compile bot.py && python -m unittest discover -s tests -v
CMD ["python", "bot.py"]
