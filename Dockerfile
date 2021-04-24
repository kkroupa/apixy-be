FROM python:3.9-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .

RUN pip install -r requirements.txt

COPY . .

RUN useradd user
# to allow installing pip packages with the image already built
RUN mkdir -p /home/user && chown user:user /home/user
USER user

CMD ["sh", "-c", "uvicorn apixy.app:app --host 0.0.0.0"]
