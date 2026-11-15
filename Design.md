---

## Security:

- Should limit the incoming message size to 60MB
- Validate the Pdf, Zip, Png, and Jpeg content types and magic bytes on respective endpoints

## Design:

- Use open source tools: qpdf, PDFium, and ImageMagick, and follow their licensing
- Follow C# minimal API modern conventions
- Optimized for throughput. This application should be able to serve an organization for PDF api needs

## Architecture:

- Modern .NET 10 C#
- Wolverine Endpoints
- Have a Dockerfile built

## Endpoints:

/HtmlToPdf: Input HtmlToPdfRequest (see below)
/WordToPdf: Input Docx file
/Compress: Input Pdf file with form based int quality (does not rasterize pdf)
/ImageCompress: Input Pdf file with form based int quality (rasterizes input pdf and outputs to pdf)
/Merge: Input form file collection of PDFs and outputs merged PDF
/ImageToPdf: Input Jpeg or Png and outputs PDF

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
