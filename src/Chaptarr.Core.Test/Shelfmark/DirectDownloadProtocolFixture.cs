using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using NLog;
using NUnit.Framework;
using NzbDrone.Core.Blocklisting;
using NzbDrone.Core.Books;
using NzbDrone.Core.Indexers;
using NzbDrone.Core.Parser.Model;
using NzbDrone.Core.Profiles.Delay;

namespace Chaptarr.Core.Test.Shelfmark
{
    [TestFixture]
    public class DirectDownloadProtocolFixture
    {
        [Test]
        public void new_delay_profiles_allow_direct_downloads_without_delay()
        {
            var profile = new DelayProfile { UsenetDelay = 60, TorrentDelay = 120 };

            Assert.That(profile.IsProtocolEnabled(DownloadProtocol.DirectDownload), Is.True);
            Assert.That(profile.GetProtocolDelay(DownloadProtocol.DirectDownload), Is.EqualTo(0));
        }

        [Test]
        public void delay_profiles_keep_each_protocol_separate()
        {
            var profile = new DelayProfile
            {
                EnableUsenet = true,
                EnableTorrent = false,
                EnableDirectDownload = false,
                UsenetDelay = 60,
                TorrentDelay = 120,
                DirectDownloadDelay = 30
            };

            Assert.That(profile.IsProtocolEnabled(DownloadProtocol.Usenet), Is.True);
            Assert.That(profile.IsProtocolEnabled(DownloadProtocol.Torrent), Is.False);
            Assert.That(profile.IsProtocolEnabled(DownloadProtocol.DirectDownload), Is.False);
            Assert.That(profile.GetProtocolDelay(DownloadProtocol.Usenet), Is.EqualTo(60));
            Assert.That(profile.GetProtocolDelay(DownloadProtocol.Torrent), Is.EqualTo(120));
            Assert.That(profile.GetProtocolDelay(DownloadProtocol.DirectDownload), Is.EqualTo(30));
        }

        [Test]
        public void a_blocklisted_direct_download_matches_the_same_title_from_the_same_indexer()
        {
            var subject = BuildBlocklist(new Blocklist
            {
                SourceTitle = "Andy Weir - Project Hail Mary [epub]",
                Protocol = DownloadProtocol.DirectDownload,
                Indexer = "Shelfmark"
            });

            Assert.That(subject.Blocklisted(1, DirectRelease("Shelfmark")), Is.True);
            Assert.That(subject.Blocklisted(1, DirectRelease("Other Shelfmark")), Is.False);
        }

        [Test]
        public void a_blocklisted_usenet_release_does_not_block_a_direct_download()
        {
            var subject = BuildBlocklist(new Blocklist
            {
                SourceTitle = "Andy Weir - Project Hail Mary [epub]",
                Protocol = DownloadProtocol.Usenet,
                Indexer = "Shelfmark",
                PublishedDate = DateTime.UtcNow
            });

            Assert.That(subject.Blocklisted(1, DirectRelease("Shelfmark")), Is.False);
        }

        private static ReleaseInfo DirectRelease(string indexer)
        {
            return new ReleaseInfo
            {
                Title = "Andy Weir - Project Hail Mary [epub]",
                Indexer = indexer,
                DownloadProtocol = DownloadProtocol.DirectDownload,
                PublishDate = DateTime.UtcNow
            };
        }

        private static BlocklistService BuildBlocklist(Blocklist entry)
        {
            var repository = DispatchProxy.Create<IBlocklistRepository, TitleRepository>();
            ((TitleRepository)(object)repository).Entries.Add(entry);

            return new BlocklistService(
                repository,
                DispatchProxy.Create<IAuthorService, ThrowingProxy<IAuthorService>>(),
                DispatchProxy.Create<IBookService, ThrowingProxy<IBookService>>(),
                LogManager.GetCurrentClassLogger());
        }

        public class TitleRepository : DispatchProxy
        {
            public List<Blocklist> Entries { get; } = new();

            protected override object Invoke(MethodInfo targetMethod, object[] args)
            {
                if (targetMethod?.Name == nameof(IBlocklistRepository.BlocklistedByTitle))
                {
                    return Entries.Where(e => e.SourceTitle == (string)args[1]).ToList();
                }

                throw new NotImplementedException($"Test proxy does not implement IBlocklistRepository.{targetMethod?.Name}");
            }
        }

        public class ThrowingProxy<T> : DispatchProxy
            where T : class
        {
            protected override object Invoke(MethodInfo targetMethod, object[] args)
            {
                throw new NotImplementedException($"Test proxy does not implement {typeof(T).Name}.{targetMethod?.Name}");
            }
        }
    }
}
