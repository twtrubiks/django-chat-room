FROM python:3.8
LABEL maintainer twtrubiks
ENV PYTHONUNBUFFERED 1
RUN mkdir /django_chat_room
WORKDIR /django_chat_room
COPY . /django_chat_room/
RUN pip install -r requirements.txt