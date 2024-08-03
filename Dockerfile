FROM ubuntu:focal
ENV DEBIAN_FRONTEND=noninteractive
RUN apt update -y && apt install -y curl jq wget axel aria2 unace unrar zip unzip p7zip-full p7zip-rar sharutils rar uudeview mpack arj cabextract rename liblzma-dev brotli lz4 python-is-python3 python3 python3-dev python3-pip git gawk sudo cpio
RUN python3 -m pip install backports.lzma protobuf pycrypto aospdtgen extract-dtb dumpyara gdown
COPY extract_and_push.sh /usr/local/bin/extract_and_push
WORKDIR /dumpyara
ENTRYPOINT extract_and_push
