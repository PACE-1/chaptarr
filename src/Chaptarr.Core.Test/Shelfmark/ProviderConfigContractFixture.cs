using System;
using System.Linq;
using NUnit.Framework;
using NzbDrone.Core.ThingiProvider;

namespace Chaptarr.Core.Test.Shelfmark
{
    [TestFixture]
    public class ProviderConfigContractFixture
    {
        [Test]
        public void provider_settings_class_names_are_unique()
        {
            // Saved providers name their settings class only by its short name (ConfigContract), so two
            // settings classes with the same name break loading every indexer, client and notification.
            var duplicates = typeof(IProviderConfig).Assembly.GetTypes()
                .Where(t => typeof(IProviderConfig).IsAssignableFrom(t) && t.IsClass && !t.IsAbstract)
                .GroupBy(t => t.Name, StringComparer.OrdinalIgnoreCase)
                .Where(g => g.Count() > 1)
                .Select(g => string.Join(", ", g.Select(t => t.FullName)))
                .ToList();

            Assert.That(duplicates, Is.Empty);
            Assert.That(ProviderConfigTypeCache.Find("ShelfmarkIndexerSettings"), Is.Not.Null);
            Assert.That(ProviderConfigTypeCache.Find("ShelfmarkDownloadClientSettings"), Is.Not.Null);
        }
    }
}
