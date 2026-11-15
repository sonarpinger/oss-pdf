FROM mcr.microsoft.com/dotnet/sdk:10.0-noble AS build
WORKDIR /src
COPY src/OssPdf/OssPdf.csproj src/OssPdf/packages.lock.json ./
RUN dotnet restore --locked-mode
COPY src/OssPdf/ ./
RUN dotnet publish -c Release --no-restore -o /out

FROM mcr.microsoft.com/dotnet/aspnet:10.0-noble AS qpdf-build
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl cmake ninja-build g++ \
        libjpeg-dev libssl-dev zlib1g-dev libpng-dev && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /work
RUN curl -fsSL --retry 3 https://github.com/qpdf/qpdf/releases/download/v12.4.2/qpdf-12.4.2.tar.gz -o qpdf.tar.gz && \
    printf '%s\n' '8a58af5b6141319287c1883bec8bd1bd545b7567b7fc5e6ce5d25a1c85f36397  qpdf.tar.gz' | sha256sum -c - && \
    mkdir qpdf && tar -xzf qpdf.tar.gz -C qpdf --strip-components=1 && \
    cmake -S qpdf -B qpdf-build -G Ninja -DCMAKE_BUILD_TYPE=Release -DBUILD_DOC=OFF \
        -DBUILD_SHARED_LIBS=OFF -DREQUIRE_CRYPTO_OPENSSL=ON && \
    cmake --build qpdf-build --target qpdf --parallel 4 && \
    install -Dm755 qpdf-build/qpdf/qpdf /out/usr/local/bin/qpdf && \
    strip /out/usr/local/bin/qpdf && \
    install -Dm644 qpdf/LICENSE.txt /out/usr/local/share/doc/qpdf/LICENSE.txt && \
    install -Dm644 qpdf/NOTICE.md /out/usr/local/share/doc/qpdf/NOTICE.md

FROM qpdf-build AS pdf-tools
RUN case "$(dpkg --print-architecture)" in \
        amd64) archive=pdfium-linux-x64.tgz; sha=384ab5c51882408a143a21f9c14dcd2caf1f75280ae41c554d0489b46c3dba9f ;; \
        arm64) archive=pdfium-linux-arm64.tgz; sha=c6ed1a91234aede0cfafdc09c401b83bd44ac69deafc4599fda75d511c36403e ;; \
        *) printf '%s\n' 'Unsupported PDFium architecture' >&2; exit 1 ;; \
    esac && \
    curl -fsSL --retry 3 "https://github.com/bblanchon/pdfium-binaries/releases/download/chromium/8152/$archive" -o "$archive" && \
    printf '%s  %s\n' "$sha" "$archive" | sha256sum -c - && \
    mkdir pdfium && tar -xzf "$archive" -C pdfium && \
    install -Dm755 pdfium/lib/libpdfium.so /out/usr/local/lib/oss-pdf/libpdfium.so && \
    install -Dm644 pdfium/LICENSE /out/usr/local/share/doc/pdfium/LICENSE && \
    cp -a pdfium/licenses /out/usr/local/share/doc/pdfium/
COPY docker/pdfium-render.c ./
RUN cc -O2 -Wall -Wextra -Werror -fstack-protector-strong -D_FORTIFY_SOURCE=2 \
        -I pdfium/include -L pdfium/lib -Wl,-rpath,/usr/local/lib/oss-pdf \
        -o /out/usr/local/bin/pdfium-render pdfium-render.c -lpdfium -lpng -lm

FROM mcr.microsoft.com/dotnet/aspnet:10.0-noble AS runtime-deps
ENV ASPNETCORE_HTTP_PORTS=8080 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    Pdf__ScratchRoot=/scratch
COPY docker/repack-libreoffice-core.sh /tmp/repack-libreoffice-core.sh
RUN apt-get update && \
    office_dir="$(mktemp -d /tmp/oss-pdf-office.XXXXXX)" && cd "$office_dir" && \
    chown _apt "$office_dir" && chmod 0755 "$office_dir" && \
    apt-get download libreoffice-core && \
    sh /tmp/repack-libreoffice-core.sh libreoffice-core_*.deb && \
    apt-get install -y --no-install-recommends imagemagick libreoffice-writer ./libreoffice-core-writer-only.deb \
        libjpeg-turbo8 libpng16-16t64 libssl3t64 zlib1g \
        fonts-dejavu-core fonts-liberation fonts-crosextra-carlito fonts-crosextra-caladea && \
    cd / && rm -rf "$office_dir" /var/lib/apt/lists/* && \
    unlink /tmp/repack-libreoffice-core.sh && \
    install -d -m 0700 -o app -g app /scratch
ENV TMPDIR=/scratch
COPY docker/imagemagick-policy.xml /etc/ImageMagick-6/policy.xml
FROM runtime-deps AS runtime
WORKDIR /app
COPY --from=build /out ./
RUN TMPDIR=/tmp dotnet OssPdf.dll --install-chromium && \
    chmod a+rx .playwright/node/*/node && \
    rm -rf /var/lib/apt/lists/* && \
    ! dpkg-query -W -f='${db:Status-Abbrev} ${binary:Package}\n' | grep -Eq '^ii +([^ ]*poppler|ghostscript|libgs[0-9])'
COPY --from=pdf-tools /out/ /
COPY LICENSE /app/LICENSE
USER app
EXPOSE 8080
ENTRYPOINT ["dotnet", "OssPdf.dll"]
