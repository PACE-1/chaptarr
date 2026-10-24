using System;
using System.IO;
using NzbDrone.Common.Disk;
using NzbDrone.Common.EnvironmentInfo;
using SkiaSharp;

namespace NzbDrone.Core.MediaCover
{
    public interface IImageResizer
    {
        void Resize(string source, string destination, int height);
    }

    public class ImageResizer : IImageResizer
    {
        // Covers come from remote metadata providers; refuse anything that would decode to an
        // absurd bitmap instead of letting one bad image exhaust memory.
        private const long MaxSourcePixels = 100_000_000;

        // Thumbnails don't need super high quality
        private const int Quality = 95;

        private readonly IDiskProvider _diskProvider;

        public ImageResizer(IDiskProvider diskProvider, IPlatformInfo platformInfo)
        {
            _diskProvider = diskProvider;
        }

        public void Resize(string source, string destination, int height)
        {
            try
            {
                // Pick the encoder from the destination extension. The resizer is shared by author,
                // narrator, and book covers; writing JPEG bytes into a .png/.webp filename produces
                // invalid HTTP content.
                var format = GetFormat(destination);

                using var codec = SKCodec.Create(source) ?? throw new InvalidDataException($"Unrecognised image '{source}'");

                if ((long)codec.Info.Width * codec.Info.Height > MaxSourcePixels)
                {
                    throw new InvalidDataException($"Image '{source}' is too large to resize ({codec.Info.Width}x{codec.Info.Height})");
                }

                using var original = SKBitmap.Decode(codec) ?? throw new InvalidDataException($"Could not decode image '{source}'");

                var width = Math.Max(1, (int)Math.Round(original.Width * (double)height / original.Height));
                var info = new SKImageInfo(width, height, original.ColorType, original.AlphaType, original.ColorSpace);

                using var resized = original.Resize(info, new SKSamplingOptions(SKCubicResampler.Mitchell)) ?? throw new InvalidDataException($"Could not resize image '{source}'");
                using var image = SKImage.FromBitmap(resized);
                using var data = image.Encode(format, Quality) ?? throw new InvalidDataException($"Could not encode '{destination}'");
                using var output = File.Create(destination);

                data.SaveTo(output);
            }
            catch
            {
                if (_diskProvider != null && _diskProvider.FileExists(destination))
                {
                    _diskProvider.DeleteFile(destination);
                }

                throw;
            }
        }

        private static SKEncodedImageFormat GetFormat(string destination)
        {
            switch (Path.GetExtension(destination).ToLowerInvariant())
            {
                case ".png":
                    return SKEncodedImageFormat.Png;
                case ".jpg":
                case ".jpeg":
                case ".jfif":
                    return SKEncodedImageFormat.Jpeg;
                case ".webp":
                    return SKEncodedImageFormat.Webp;
                default:
                    throw new NotSupportedException($"Cannot write a resized image as '{Path.GetExtension(destination)}'");
            }
        }
    }
}
