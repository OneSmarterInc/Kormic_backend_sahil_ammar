FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# --home explicitly, not the --system default: Debian's adduser --system
# with no --home set the account's home directory to the literal path
# "/nonexistent" -- anything that writes to $HOME (gunicorn included) then
# fails with Permission denied. /home/app lives outside the bind-mounted
# /app, so it stays valid regardless of the host directory's ownership.
RUN addgroup --system app && adduser --system --home /home/app --ingroup app app \
    && mkdir -p /home/app /app/uploads /app/staticfiles \
    && chown -R app:app /home/app /app
ENV HOME=/home/app
USER app

EXPOSE 8030
