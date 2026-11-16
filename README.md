1. bitcoin-core 릴리즈 tarball 다운로드 및 해제
```
curl -O https://bitcoin.org/bin/bitcoin-core-31.0/bitcoin-31.0-x86_64-linux-gnu.tar.gz
tar -xf bitcoin-31.0-x86_64-linux-gnu.tar.gz -C /opt
```

2. 심링크
```
ln -sfn /opt/bitcoin-31.0 /opt/bitcoin
ln -s /opt/bitcoin/bin/* /usr/local/bin
```

3. [bitcoin-core 소스트리](https://github.com/bitcoin/bitcoin)의
   [contrib/init/bitcoind.service](https://github.com/bitcoin/bitcoin/blob/master/contrib/init/bitcoind.service)
   파일 받아오기 및 패치
```
curl -LO https://github.com/bitcoin/bitcoin/raw/refs/heads/master/contrib/init/bitcoind.service \
     --output-dir /etc/systemd/system/
systemctl daemon-reload
systemctl edit --stdin bitcoind <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/local/bin/bitcoind -pid=/run/bitcoind/bitcoind.pid \
                                  -conf=/etc/bitcoin/bitcoin.conf \
                                  -datadir=/home/bitcoin \
                                  -startupnotify='systemd-notify --ready' \
                                  -shutdownnotify='systemd-notify --stopping'
EOF
```

4. Service용 유저 생성
```
useradd --system --home-dir /home/bitcoin --shell /usr/sbin/nologin bitcoin
```

5. 기본 설정 파일 복사
```
mkdir -p /etc/bitcoin/
cp /opt/bitcoin/bitcoin.conf /etc/bitcoin/
```
