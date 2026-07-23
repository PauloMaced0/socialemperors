# Nginx reverse proxy

Run the application on loopback:

```sh
SE_BIND=127.0.0.1 SE_PORT=5050 python3 server.py
```

Then use this Nginx site:

```nginx
server {
    listen 80;
    listen [::]:80;

    server_name social-empires.local;

    location / {
        proxy_pass http://127.0.0.1:5050;
        proxy_http_version 1.1;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

The Flask application trusts one reverse-proxy hop and uses the forwarded
scheme and host when generating the Ruffle loader, `swftoload`, `staticUrl`
and `dynamicUrl` values. Nginx therefore does not need `sub_filter`,
`sub_filter_once`, or the `Accept-Encoding` workaround.

Keep Flask bound to `127.0.0.1` when using this configuration. If TLS is added
at Nginx, the same forwarded-protocol header makes the generated URLs use
`https://`.
