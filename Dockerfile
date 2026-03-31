FROM ubuntu:focal
ENV DEBIAN_FRONTEND=noninteractive
RUN apt update -y && apt install -y curl jq wget axel aria2 unace unrar zip unzip p7zip-full p7zip-rar sharutils rar uudeview mpack arj cabextract rename liblzma-dev brotli lz4 python-is-python3 python3 python3-dev python3-pip git gawk sudo cpio
RUN python3 -m pip install python-telegram-bot[job-queue] backports.lzma protobuf pycrypto aospdtgen extract-dtb dumpyara gdown git+https://github.com/Juvenal-Yescas/mediafire-dl arq redis
COPY run_arq_worker.py /usr/local/bin/
COPY worker_settings.py /usr/local/bin/
COPY dumpyarabot/ /app/dumpyarabot/
WORKDIR /dumpyara
ENTRYPOINT ["python3", "/usr/local/bin/run_arq_worker.py"]
