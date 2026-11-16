using System.Globalization;
using System.IO.Compression;
using System.Text;
using System.Xml;
using Microsoft.AspNetCore.WebUtilities;
using Microsoft.Net.Http.Headers;

namespace OssPdf;

internal enum UploadKind { Pdf, Word, Image }

internal sealed record UploadBatch(IReadOnlyList<string> Paths, int? Quality, long? ImagePdfTargetBytes);

internal static class Uploads
{
    private const string DocxType = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
    private const string WordMainType = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml";

    public static async Task<UploadBatch> ReadAsync(
        MultipartReader reader,
        PdfJob job,
        UploadKind kind,
        bool needsQuality,
        int maxFiles,
        CancellationToken cancellationToken)
    {
        var paths = new List<string>();
        int? quality = null;
        var smallImagePdf = false;
        long? targetBytes = null;
        try
        {
            while (await reader.ReadNextSectionAsync(cancellationToken) is { } section)
            {
                if (!ContentDispositionHeaderValue.TryParse(section.ContentDisposition, out var disposition) ||
                    !disposition.DispositionType.Equals("form-data", StringComparison.OrdinalIgnoreCase))
                {
                    throw InvalidForm();
                }

                var field = HeaderUtilities.RemoveQuotes(disposition.Name).Value;
                var isFile = disposition.FileName.HasValue || disposition.FileNameStar.HasValue;
                if (isFile && field == (maxFiles > 1 ? "files" : "file") && paths.Count < maxFiles)
                {
                    var extension = GetExtension(kind, section.ContentType);
                    var path = job.PathFor($"input-{paths.Count:D3}{extension}");
                    await using (var destination = File.Create(path))
                    {
                        await section.Body.CopyToAsync(destination, cancellationToken);
                    }

                    await CheckSignatureAsync(path, kind, cancellationToken);
                    paths.Add(path);
                    continue;
                }

                if (!isFile && field == "quality" && needsQuality && quality is null)
                {
                    quality = await ReadQualityAsync(section.Body, cancellationToken);
                    continue;
                }

                if (!isFile && field == "profile" && kind == UploadKind.Image && !smallImagePdf)
                {
                    var profile = new byte[6];
                    var count = await section.Body.ReadAtLeastAsync(profile, profile.Length, false, cancellationToken);
                    if (!profile.AsSpan(0, count).SequenceEqual("small"u8))
                    {
                        throw new PdfRequestException(400, "Image PDF profile must be small.");
                    }

                    smallImagePdf = true;
                    continue;
                }

                if (!isFile && field == "targetBytes" && kind == UploadKind.Image && targetBytes is null)
                {
                    targetBytes = await ReadTargetBytesAsync(section.Body, cancellationToken);
                    continue;
                }

                throw InvalidForm();
            }
        }
        catch (InvalidDataException)
        {
            throw InvalidForm();
        }

        var minFiles = maxFiles > 1 ? 2 : 1;
        if (paths.Count < minFiles || needsQuality && quality is null)
        {
            throw InvalidForm();
        }

        if (targetBytes.HasValue && !smallImagePdf)
        {
            throw new PdfRequestException(400, "targetBytes requires profile=small.");
        }

        return new UploadBatch(paths, quality, smallImagePdf ? targetBytes ?? PdfLimits.DefaultSmallImagePdfTargetBytes : null);
    }

    private static string GetExtension(UploadKind kind, string? contentType)
    {
        if (!MediaTypeHeaderValue.TryParse(contentType, out var header))
        {
            throw UnsupportedFileType();
        }

        return (kind, header.MediaType.Value?.ToLowerInvariant()) switch
        {
            (UploadKind.Pdf, "application/pdf") => ".pdf",
            (UploadKind.Word, DocxType or "application/zip") => ".docx",
            (UploadKind.Image, "image/jpeg") => ".jpg",
            (UploadKind.Image, "image/png") => ".png",
            _ => throw UnsupportedFileType()
        };
    }

    internal static async Task CheckSignatureAsync(string path, UploadKind kind, CancellationToken cancellationToken)
    {
        await using var stream = File.OpenRead(path);
        var header = new byte[8];
        var count = await stream.ReadAtLeastAsync(header, header.Length, false, cancellationToken);
        var valid = kind switch
        {
            UploadKind.Pdf => header.AsSpan(0, count).StartsWith("%PDF-"u8),
            UploadKind.Word => header.AsSpan(0, count).StartsWith(new byte[] { 0x50, 0x4B, 0x03, 0x04 }),
            UploadKind.Image when Path.GetExtension(path) == ".png" =>
                header.AsSpan(0, count).SequenceEqual(new byte[] { 0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A }),
            UploadKind.Image => header.AsSpan(0, count).StartsWith(new byte[] { 0xFF, 0xD8, 0xFF }),
            _ => false
        };
        if (!valid)
        {
            throw UnsupportedFileType();
        }
    }

    public static void CheckWordPackage(string path, CancellationToken cancellationToken)
    {
        const int maxEntries = 4096;
        const long maxExpandedBytes = 256 * 1024 * 1024;
        const long maxControlFileBytes = 1024 * 1024;
        try
        {
            using var archive = ZipFile.OpenRead(path);
            if (archive.Entries.Count > maxEntries)
            {
                throw InvalidWord();
            }

            var names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            long remainingBytes = maxExpandedBytes;
            foreach (var entry in archive.Entries)
            {
                cancellationToken.ThrowIfCancellationRequested();
                if (entry.Length < 0 || entry.Length > remainingBytes || !names.Add(entry.FullName) ||
                    entry.FullName.Contains("vbaProject", StringComparison.OrdinalIgnoreCase))
                {
                    throw InvalidWord();
                }

                remainingBytes -= entry.Length;
                if (entry.FullName.EndsWith(".rels", StringComparison.OrdinalIgnoreCase))
                {
                    CheckRelationships(entry, maxControlFileBytes);
                }
            }

            if (archive.GetEntry("word/document.xml") is null || archive.GetEntry("_rels/.rels") is null ||
                archive.GetEntry("[Content_Types].xml") is not { } contentTypes)
            {
                throw InvalidWord();
            }

            using var types = ReadControlXml(contentTypes, maxControlFileBytes);
            while (types.Read())
            {
                if (types.NodeType == XmlNodeType.Element && types.LocalName == "Override" &&
                    types.GetAttribute("PartName") == "/word/document.xml" &&
                    types.GetAttribute("ContentType") == WordMainType)
                {
                    return;
                }
            }
        }
        catch (Exception exception) when (exception is InvalidDataException or XmlException)
        {
            throw InvalidWord();
        }

        throw InvalidWord();
    }

    private static void CheckRelationships(ZipArchiveEntry entry, long maxBytes)
    {
        using var reader = ReadControlXml(entry, maxBytes);
        while (reader.Read())
        {
            if (reader.NodeType == XmlNodeType.Element && reader.LocalName == "Relationship" &&
                string.Equals(reader.GetAttribute("TargetMode"), "External", StringComparison.OrdinalIgnoreCase) &&
                reader.GetAttribute("Type") is not (
                    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" or
                    "http://purl.oclc.org/ooxml/officeDocument/relationships/hyperlink"))
            {
                throw InvalidWord();
            }
        }
    }

    private static XmlReader ReadControlXml(ZipArchiveEntry entry, long maxBytes)
    {
        if (entry.Length > maxBytes)
        {
            throw InvalidWord();
        }

        return XmlReader.Create(entry.Open(), new XmlReaderSettings
        {
            CloseInput = true,
            DtdProcessing = DtdProcessing.Prohibit,
            MaxCharactersInDocument = maxBytes
        });
    }

    private static async Task<int> ReadQualityAsync(Stream stream, CancellationToken cancellationToken)
    {
        var bytes = new byte[4];
        var count = await stream.ReadAtLeastAsync(bytes, bytes.Length, false, cancellationToken);
        if (count < bytes.Length && int.TryParse(Encoding.ASCII.GetString(bytes, 0, count), NumberStyles.None,
                CultureInfo.InvariantCulture, out var quality) && quality is >= 1 and <= 100)
        {
            return quality;
        }

        throw new PdfRequestException(400, "Quality must be an integer from 1 to 100.");
    }

    private static async Task<long> ReadTargetBytesAsync(Stream stream, CancellationToken cancellationToken)
    {
        var bytes = new byte[20];
        var count = await stream.ReadAtLeastAsync(bytes, bytes.Length, false, cancellationToken);
        if (count < bytes.Length && long.TryParse(Encoding.ASCII.GetString(bytes, 0, count), NumberStyles.None,
                CultureInfo.InvariantCulture, out var targetBytes) && targetBytes > 0)
        {
            return targetBytes;
        }

        throw new PdfRequestException(400, "targetBytes must be a positive integer.");
    }

    private static PdfRequestException InvalidForm() => new(400, "The multipart form fields are missing or invalid.");
    private static PdfRequestException InvalidWord() => new(422, "The file is not a supported DOCX document.");
    private static PdfRequestException UnsupportedFileType() => new(415, "The file type or signature is unsupported.");
}
