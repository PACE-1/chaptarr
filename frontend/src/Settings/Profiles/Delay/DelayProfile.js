import classNames from 'classnames';
import PropTypes from 'prop-types';
import React, { Component } from 'react';
import Icon from 'Components/Icon';
import Link from 'Components/Link/Link';
import ConfirmModal from 'Components/Modal/ConfirmModal';
import TagList from 'Components/TagList';
import { icons, kinds } from 'Helpers/Props';
import getProtocolName from 'Utilities/String/getProtocolName';
import translate from 'Utilities/String/translate';
import EditDelayProfileModalConnector from './EditDelayProfileModalConnector';
import styles from './DelayProfile.css';

function getDelay(enabled, delay) {
  if (!enabled) {
    return '-';
  }

  if (!delay) {
    return 'No Delay';
  }

  if (delay === 1) {
    return '1 Minute';
  }

  // TODO: use better units of time than just minutes
  return `${delay} Minutes`;
}

class DelayProfile extends Component {

  //
  // Lifecycle

  constructor(props, context) {
    super(props, context);

    this.state = {
      isEditDelayProfileModalOpen: false,
      isDeleteDelayProfileModalOpen: false
    };
  }

  //
  // Listeners

  onEditDelayProfilePress = () => {
    this.setState({ isEditDelayProfileModalOpen: true });
  };

  onEditDelayProfileModalClose = () => {
    this.setState({ isEditDelayProfileModalOpen: false });
  };

  onDeleteDelayProfilePress = () => {
    this.setState({
      isEditDelayProfileModalOpen: false,
      isDeleteDelayProfileModalOpen: true
    });
  };

  onDeleteDelayProfileModalClose = () => {
    this.setState({ isDeleteDelayProfileModalOpen: false });
  };

  onConfirmDeleteDelayProfile = () => {
    this.props.onConfirmDeleteDelayProfile(this.props.id);
  };

  //
  // Render

  render() {
    const {
      id,
      enableUsenet,
      enableTorrent,
      enableDirectDownload,
      preferredProtocol,
      usenetDelay,
      torrentDelay,
      directDownloadDelay,
      tags,
      tagList,
      isDragging,
      connectDragSource
    } = this.props;

    // Older profiles may not have enableDirectDownload set; treat that as enabled.
    const isDirectDownloadEnabled = enableDirectDownload !== false;

    const enabledProtocols = [
      enableUsenet && 'usenet',
      enableTorrent && 'torrent',
      isDirectDownloadEnabled && 'directDownload'
    ].filter(Boolean);

    let preferred = getProtocolName(
      enabledProtocols.includes(preferredProtocol) || !enabledProtocols.length ?
        preferredProtocol :
        enabledProtocols[0]
    );

    if (enabledProtocols.length === 1) {
      preferred = translate('DelayProfileOnlyProtocol', {
        protocol: getProtocolName(enabledProtocols[0])
      });
    }

    return (
      <div
        className={classNames(
          styles.delayProfile,
          isDragging && styles.isDragging
        )}
      >
        <div className={styles.column}>{preferred}</div>
        <div className={styles.column}>{getDelay(enableUsenet, usenetDelay)}</div>
        <div className={styles.column}>{getDelay(enableTorrent, torrentDelay)}</div>
        <div className={styles.column}>{getDelay(isDirectDownloadEnabled, directDownloadDelay)}</div>

        <TagList
          tags={tags}
          tagList={tagList}
        />

        <div className={styles.actions}>
          <Link
            className={id === 1 ? styles.editButton : undefined}
            onPress={this.onEditDelayProfilePress}
          >
            <Icon name={icons.EDIT} />
          </Link>

          {
            id !== 1 &&
              connectDragSource(
                <div className={styles.dragHandle}>
                  <Icon
                    className={styles.dragIcon}
                    name={icons.REORDER}
                  />
                </div>
              )
          }
        </div>

        <EditDelayProfileModalConnector
          id={id}
          isOpen={this.state.isEditDelayProfileModalOpen}
          onModalClose={this.onEditDelayProfileModalClose}
          onDeleteDelayProfilePress={this.onDeleteDelayProfilePress}
        />

        <ConfirmModal
          isOpen={this.state.isDeleteDelayProfileModalOpen}
          kind={kinds.DANGER}
          title={translate('DeleteDelayProfile')}
          message={translate('DeleteDelayProfileMessageText')}
          confirmLabel={translate('Delete')}
          onConfirm={this.onConfirmDeleteDelayProfile}
          onCancel={this.onDeleteDelayProfileModalClose}
        />
      </div>
    );
  }
}

DelayProfile.propTypes = {
  id: PropTypes.number.isRequired,
  enableUsenet: PropTypes.bool.isRequired,
  enableTorrent: PropTypes.bool.isRequired,
  enableDirectDownload: PropTypes.bool,
  preferredProtocol: PropTypes.string.isRequired,
  usenetDelay: PropTypes.number.isRequired,
  torrentDelay: PropTypes.number.isRequired,
  directDownloadDelay: PropTypes.number,
  tags: PropTypes.arrayOf(PropTypes.number).isRequired,
  tagList: PropTypes.arrayOf(PropTypes.object).isRequired,
  isDragging: PropTypes.bool.isRequired,
  connectDragSource: PropTypes.func,
  onConfirmDeleteDelayProfile: PropTypes.func.isRequired
};

DelayProfile.defaultProps = {
  // The drag preview will not connect the drag handle.
  connectDragSource: (node) => node
};

export default DelayProfile;
