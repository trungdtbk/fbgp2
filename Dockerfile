FROM mikenowak/exabgp

RUN apk --no-cache add py-pip python3-dev gcc musl-dev
RUN mkdir /fbgp && mkdir /etc/fbgp && mkdir /var/log/fbgp
COPY . /fbgp
RUN pip3 install --upgrade pip
RUN cd /fbgp/faucet && pip3 install -r requirements.txt && PBR_VERSION=1.8.33 python3 setup.py install
RUN cd /fbgp && python3 setup.py install
WORKDIR /

#ADD tests/faucet.yaml /etc/fbgp
#ADD tests/fbgp.yaml /etc/fbgp
VOLUME [ "/etc/fbgp", "/var/log/fbgp" ]

ENV FAUCET_CONFIG=/etc/fbgp/faucet.yaml
ENV FAUCET_LOG=/var/log/fbgp/faucet.log
ENV FAUCET_EXCEPTION_LOG=/var/log/fbgp/faucet_exception.log
ENV FAUCET_EVENT_SOCK=/var/log/fbgp/faucet.sock

ENV FBGP_CONFIG=/etc/fbgp/fbgp.yaml
ENV FBGP_EXABGP_CONFIG=/etc/fbgp/exabgp.conf
ENV FBGP_LOG=/var/log/fbgp/fbgp.log
ENV FBGP_EXABGP_CONNECT_LOG=/var/log/fbgp/exabgp_connect.log
ENV FBGP_EXABGP_SOCK=/var/log/fbgp/exabgp.sock
ENV FBGP_EXABGP_HOOK_LOG=/var/log/fbgp/exabgp_hook.log

EXPOSE 1179 6653

CMD ["ryu-manager", "faucet.faucet", "fbgp.fbgp"]
