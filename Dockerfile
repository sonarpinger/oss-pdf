FROM mcr.microsoft.com/dotnet/sdk:10.0-noble AS build
WORKDIR /src
COPY src/OssPdf/OssPdf.csproj src/OssPdf/packages.lock.json ./
RUN dotnet restore --locked-mode
COPY src/OssPdf/ ./
RUN dotnet publish -c Release --no-restore -o /out

FROM mcr.microsoft.com/dotnet/aspnet:10.0-noble AS runtime-deps
ENV ASPNETCORE_HTTP_PORTS=8080 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    Pdf__ScratchRoot=/scratch \
    TMPDIR=/scratch
RUN apt-get update && \
    apt-get install -y --no-install-recommends ghostscript imagemagick libreoffice-writer \
        qpdf poppler-utils fonts-dejavu-core fonts-liberation fonts-crosextra-carlito \
        fonts-crosextra-caladea && \
    rm -rf /var/lib/apt/lists/* && \
    install -d -m 0700 -o app -g app /scratch
COPY docker/imagemagick-policy.xml /etc/ImageMagick-6/policy.xml
FROM runtime-deps AS runtime
WORKDIR /app
COPY --from=build /out ./
RUN TMPDIR=/tmp dotnet OssPdf.dll --install-chromium && \
    chmod a+rx .playwright/node/*/node && \
    rm -rf /var/lib/apt/lists/*
COPY LICENSE /app/LICENSE
USER app
EXPOSE 8080
ENTRYPOINT ["dotnet", "OssPdf.dll"]
