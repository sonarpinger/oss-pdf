---

## Security:

- Should limit the incoming message size to 60MB
- Validate the Pdf, Zip, Png, and Jpeg content types and magic bytes on respective endpoints

## Design:

- Use open source tools such as qpdf, ImageMagick, and SkiaSharp, and follow their licensing
- Follow C# minimal API modern conventions
- Optimized for throughput. This application should be able to serve an organization for PDF api needs

## Architecture:

- Modern .NET 10 C#
- ASP.NET Core Minimal APIs
- Have a Dockerfile built

## Endpoints:

/HtmlToPdf: Input HtmlToPdfRequest (see below)
/WordToPdf: Input Docx file
/Compress: Input Pdf file with form based int quality (does not rasterize pdf)
/Merge: Input form file collection of PDFs and outputs merged PDF
/ImageToPdf: Input Jpeg or Png and outputs PDF. Optional `profile=small` downsamples the image at the same page size, with a best-effort `targetBytes` default of 100,000 bytes.

```csharp
public sealed record HtmlToPdfRequest(
	string Body,
	string? Header,
	string? Footer,
	string? FileName,
	double? MarginTop,
	double? MarginBottom,
	double? MarginRight,
	double? MarginLeft
)
```
