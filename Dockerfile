FROM python:3.13-slim
LABEL maintainer twtrubiks
ENV PYTHONUNBUFFERED 1
RUN mkdir /django_chat_room
WORKDIR /django_chat_room
COPY . /django_chat_room/
RUN pip install --no-cache-dir -r requirements.txt

# for entry point
RUN chmod +x /django_chat_room/entrypoint.sh

# 設定 entrypoint
ENTRYPOINT ["/django_chat_room/entrypoint.sh"]