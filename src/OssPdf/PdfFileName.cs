using System.Text;

namespace OssPdf;

internal static class PdfFileName
{
    private const int MaxLength = 132;

    public static string? Normalize(string? name)
    {
        if (string.IsNullOrWhiteSpace(name))
        {
            return null;
        }

        if (name.Length > MaxLength || name.Any(character => char.IsControl(character) || character is '/' or '\\'))
        {
            throw Invalid();
        }

        name = name.Trim();
        return name.EndsWith(".pdf", StringComparison.OrdinalIgnoreCase) ? name : name + ".pdf";
    }

    public static async Task<string> ReadAsync(Stream stream, CancellationToken cancellationToken)
    {
        using var reader = new StreamReader(stream, new UTF8Encoding(false, true),
            detectEncodingFromByteOrderMarks: false, leaveOpen: true);
        var buffer = new char[MaxLength + 1];
        try
        {
            var count = await reader.ReadBlockAsync(buffer.AsMemory(), cancellationToken);
            if (count <= MaxLength)
            {
                return new string(buffer, 0, count);
            }

            // A blank name uses the default even when it exceeds the length limit.
            do
            {
                if (!buffer.AsSpan(0, count).IsWhiteSpace())
                {
                    throw Invalid();
                }

                count = await reader.ReadAsync(buffer.AsMemory(), cancellationToken);
            }
            while (count > 0);

            return string.Empty;
        }
        catch (DecoderFallbackException)
        {
            throw Invalid();
        }
    }

    private static PdfRequestException Invalid() => new(400, "The PDF filename is invalid.");
}
