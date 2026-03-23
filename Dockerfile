FROM node:22-slim AS frontend-builder

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build

FROM python:3.14-slim

WORKDIR /app
COPY pyproject.toml ./
COPY positionoracle/ positionoracle/
RUN pip install --no-cache-dir .
COPY --from=frontend-builder /frontend/build frontend/build/

RUN useradd --create-home positionoracle
RUN mkdir -p /app/data && chown positionoracle:positionoracle /app/data
USER positionoracle

EXPOSE 8000

CMD ["uvicorn", "positionoracle.main:app", "--host", "0.0.0.0", "--port", "8000"]
