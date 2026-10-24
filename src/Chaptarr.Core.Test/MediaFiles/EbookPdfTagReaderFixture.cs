using System;
using System.IO;
using System.IO.Abstractions;
using NLog;
using NUnit.Framework;
using NzbDrone.Core.MediaFiles;
using NzbDrone.Core.MediaFiles.TagExtraction;
using UglyToad.PdfPig.Content;
using UglyToad.PdfPig.Writer;

namespace Chaptarr.Core.Test.MediaFiles
{
    [TestFixture]
    public class EbookPdfTagReaderFixture
    {
        private string _folder;

        [SetUp]
        public void SetUp()
        {
            _folder = Path.Combine(Path.GetTempPath(), "chaptarr-pdf-tags-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(_folder);
        }

        [TearDown]
        public void TearDown()
        {
            Directory.Delete(_folder, recursive: true);
        }

        private static EBookTagService Subject()
        {
            return new EBookTagService(null, null, null, null, null, null, LogManager.GetCurrentClassLogger());
        }

        [Test]
        public void should_read_the_document_information_fields()
        {
            var builder = new PdfDocumentBuilder();
            builder.DocumentInformation.Title = "Throne of Glass";
            builder.DocumentInformation.Author = "Sarah J. Maas";
            builder.DocumentInformation.Subject = "A young assassin";
            builder.DocumentInformation.Keywords = "fantasy";
            builder.DocumentInformation.Creator = "Writer";
            builder.DocumentInformation.Producer = "Press";
            builder.AddPage(PageSize.A4);

            var path = Path.Combine(_folder, "book.pdf");
            File.WriteAllBytes(path, builder.Build());

            var tags = Subject().ReadAllTags(new FileSystem().FileInfo.FromFileName(path));

            Assert.Multiple(() =>
            {
                Assert.That(tags["title"], Is.EqualTo(new[] { "Throne of Glass" }));
                Assert.That(tags["author"], Is.EqualTo(new[] { "Sarah J. Maas" }));
                Assert.That(tags["authors"], Is.EqualTo(new[] { "Sarah J. Maas" }));
                Assert.That(tags["description"], Is.EqualTo(new[] { "A young assassin" }));
                Assert.That(tags["tags"], Is.EqualTo(new[] { "fantasy" }));
                Assert.That(tags["creator"], Is.EqualTo(new[] { "Writer" }));
            });
        }

        [Test]
        public void should_raise_a_tag_extraction_error_for_a_corrupt_pdf()
        {
            var path = Path.Combine(_folder, "broken.pdf");
            File.WriteAllText(path, "not a pdf");

            Assert.Throws<TagExtractionException>(() => Subject().ReadAllTags(new FileSystem().FileInfo.FromFileName(path)));
        }
    }
}
