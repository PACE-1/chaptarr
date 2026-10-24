import _ from 'lodash';
import PropTypes from 'prop-types';
import React, { Component } from 'react';
import { connect } from 'react-redux';
import { createSelector } from 'reselect';
import { saveDelayProfile, setDelayProfileValue } from 'Store/Actions/settingsActions';
import selectSettings from 'Store/Selectors/selectSettings';
import translate from 'Utilities/String/translate';
import EditDelayProfileModalContent from './EditDelayProfileModalContent';

const newDelayProfile = {
  enableUsenet: true,
  enableTorrent: true,
  enableDirectDownload: true,
  preferredProtocol: 'usenet',
  usenetDelay: 0,
  torrentDelay: 0,
  directDownloadDelay: 0,
  bypassIfHighestQuality: false,
  bypassIfAboveCustomFormatScore: false,
  minimumCustomFormatScore: 0,
  tags: []
};

const protocols = [
  { key: 'usenet', enableField: 'enableUsenet', labelKey: 'Usenet' },
  { key: 'torrent', enableField: 'enableTorrent', labelKey: 'Torrent' },
  { key: 'directDownload', enableField: 'enableDirectDownload', labelKey: 'DirectDownload' }
];

const enableFields = protocols.map((p) => p.enableField);

function getFieldValue(item, name) {
  const field = item[name];

  return field ? field.value : undefined;
}

// enableDirectDownload may be missing/null on profiles created before
// direct download support existed; treat that as enabled.
function isProtocolEnabled(item, enableField) {
  const value = getFieldValue(item, enableField);

  if (enableField === 'enableDirectDownload') {
    return value !== false;
  }

  return !!value;
}

function getEnabledProtocols(item) {
  return protocols.filter((p) => isProtocolEnabled(item, p.enableField)).map((p) => p.key);
}

function getEffectivePreferredProtocol(preferredProtocol, enabledProtocols) {
  if (enabledProtocols.includes(preferredProtocol)) {
    return preferredProtocol;
  }

  return enabledProtocols.length ? enabledProtocols[0] : preferredProtocol;
}

function wrapField(field, value) {
  return {
    errors: [],
    warnings: [],
    ...field,
    value
  };
}

function createDelayProfileSelector() {
  return createSelector(
    (state, { id }) => id,
    (state) => state.settings.delayProfiles,
    (id, delayProfiles) => {
      const {
        isFetching,
        error,
        isSaving,
        saveError,
        pendingChanges,
        items
      } = delayProfiles;

      const profile = id ? _.find(items, { id }) : newDelayProfile;
      const settings = selectSettings(profile, pendingChanges, saveError);

      return {
        id,
        isFetching,
        error,
        isSaving,
        saveError,
        item: settings.settings,
        ...settings
      };
    }
  );
}

function createMapStateToProps() {
  return createSelector(
    createDelayProfileSelector(),
    (delayProfile) => {
      const item = delayProfile.item;
      const enabledProtocols = getEnabledProtocols(item);
      const preferredProtocol = getEffectivePreferredProtocol(
        getFieldValue(item, 'preferredProtocol'),
        enabledProtocols
      );

      const directDownloadDelay = getFieldValue(item, 'directDownloadDelay');

      const preferredProtocolOptions = protocols
        .filter((p) => enabledProtocols.includes(p.key))
        .map((p) => ({ key: p.key, value: translate(p.labelKey) }));

      return {
        ...delayProfile,
        item: {
          ...item,
          enableDirectDownload: wrapField(item.enableDirectDownload, isProtocolEnabled(item, 'enableDirectDownload')),
          directDownloadDelay: wrapField(item.directDownloadDelay, directDownloadDelay == null ? 0 : directDownloadDelay),
          preferredProtocol: wrapField(item.preferredProtocol, preferredProtocol)
        },
        enabledProtocols,
        preferredProtocolOptions
      };
    }
  );
}

const mapDispatchToProps = {
  setDelayProfileValue,
  saveDelayProfile
};

class EditDelayProfileModalContentConnector extends Component {

  //
  // Lifecycle

  componentDidMount() {
    if (!this.props.id) {
      Object.keys(newDelayProfile).forEach((name) => {
        this.props.setDelayProfileValue({
          name,
          value: newDelayProfile[name]
        });
      });
    }
  }

  componentDidUpdate(prevProps, prevState) {
    if (prevProps.isSaving && !this.props.isSaving && !this.props.saveError) {
      this.props.onModalClose();
    }
  }

  //
  // Listeners

  onInputChange = ({ name, value }) => {
    this.props.setDelayProfileValue({ name, value });

    if (!enableFields.includes(name)) {
      return;
    }

    // Keep the preferred protocol pointing at an enabled protocol.
    const item = {
      ...this.props.item,
      [name]: { value }
    };

    const enabledProtocols = getEnabledProtocols(item);
    const currentPreferred = getFieldValue(item, 'preferredProtocol');
    const preferredProtocol = getEffectivePreferredProtocol(currentPreferred, enabledProtocols);

    if (preferredProtocol !== currentPreferred) {
      this.props.setDelayProfileValue({ name: 'preferredProtocol', value: preferredProtocol });
    }
  };

  onSavePress = () => {
    this.props.saveDelayProfile({ id: this.props.id });
  };

  //
  // Render

  render() {
    return (
      <EditDelayProfileModalContent
        {...this.props}
        onSavePress={this.onSavePress}
        onInputChange={this.onInputChange}
      />
    );
  }
}

EditDelayProfileModalContentConnector.propTypes = {
  id: PropTypes.number,
  isSaving: PropTypes.bool.isRequired,
  saveError: PropTypes.object,
  item: PropTypes.object.isRequired,
  setDelayProfileValue: PropTypes.func.isRequired,
  saveDelayProfile: PropTypes.func.isRequired,
  onModalClose: PropTypes.func.isRequired
};

export default connect(createMapStateToProps, mapDispatchToProps)(EditDelayProfileModalContentConnector);
