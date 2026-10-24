import PropTypes from 'prop-types';
import React from 'react';
import Alert from 'Components/Alert';
import Form from 'Components/Form/Form';
import FormGroup from 'Components/Form/FormGroup';
import FormInputGroup from 'Components/Form/FormInputGroup';
import FormLabel from 'Components/Form/FormLabel';
import Button from 'Components/Link/Button';
import SpinnerErrorButton from 'Components/Link/SpinnerErrorButton';
import LoadingIndicator from 'Components/Loading/LoadingIndicator';
import ModalBody from 'Components/Modal/ModalBody';
import ModalContent from 'Components/Modal/ModalContent';
import ModalFooter from 'Components/Modal/ModalFooter';
import ModalHeader from 'Components/Modal/ModalHeader';
import { inputTypes, kinds } from 'Helpers/Props';
import { boolSettingShape, numberSettingShape, tagSettingShape } from 'Helpers/Props/Shapes/settingShape';
import translate from 'Utilities/String/translate';
import styles from './EditDelayProfileModalContent.css';

function EditDelayProfileModalContent(props) {
  const {
    id,
    isFetching,
    error,
    isSaving,
    saveError,
    item,
    enabledProtocols,
    preferredProtocolOptions,
    onInputChange,
    onSavePress,
    onModalClose,
    onDeleteDelayProfilePress,
    ...otherProps
  } = props;

  const {
    enableUsenet,
    enableTorrent,
    enableDirectDownload,
    preferredProtocol,
    usenetDelay,
    torrentDelay,
    directDownloadDelay,
    bypassIfHighestQuality,
    bypassIfAboveCustomFormatScore,
    minimumCustomFormatScore,
    tags
  } = item;

  return (
    <ModalContent onModalClose={onModalClose}>
      <ModalHeader>
        {id ? 'Edit Delay Profile' : 'Add Delay Profile'}
      </ModalHeader>

      <ModalBody>
        {
          isFetching &&
            <LoadingIndicator />
        }

        {
          !isFetching && !!error &&
            <div>
              {translate('UnableToAddANewQualityProfilePleaseTryAgain')}
            </div>
        }

        {
          !isFetching && !error &&
            <Form {...otherProps}>
              <FormGroup>
                <FormLabel>{translate('EnableUsenet')}</FormLabel>

                <FormInputGroup
                  type={inputTypes.CHECK}
                  name="enableUsenet"
                  {...enableUsenet}
                  onChange={onInputChange}
                />
              </FormGroup>

              <FormGroup>
                <FormLabel>{translate('EnableTorrent')}</FormLabel>

                <FormInputGroup
                  type={inputTypes.CHECK}
                  name="enableTorrent"
                  {...enableTorrent}
                  onChange={onInputChange}
                />
              </FormGroup>

              <FormGroup>
                <FormLabel>{translate('EnableDirectDownload')}</FormLabel>

                <FormInputGroup
                  type={inputTypes.CHECK}
                  name="enableDirectDownload"
                  {...enableDirectDownload}
                  onChange={onInputChange}
                />
              </FormGroup>

              {
                enabledProtocols.length === 0 ?
                  <Alert kind={kinds.DANGER}>
                    {translate('DelayProfileNoProtocolEnabled')}
                  </Alert> :
                  null
              }

              {
                enabledProtocols.length > 1 ?
                  <FormGroup>
                    <FormLabel>
                      {translate('PreferredProtocol')}
                    </FormLabel>

                    <FormInputGroup
                      type={inputTypes.SELECT}
                      name="preferredProtocol"
                      {...preferredProtocol}
                      values={preferredProtocolOptions}
                      helpText={translate('PreferredProtocolHelpText')}
                      onChange={onInputChange}
                    />
                  </FormGroup> :
                  null
              }

              {
                enableUsenet.value ?
                  <FormGroup>
                    <FormLabel>
                      {translate('UsenetDelay')}
                    </FormLabel>

                    <FormInputGroup
                      type={inputTypes.NUMBER}
                      name="usenetDelay"
                      unit="minutes"
                      {...usenetDelay}
                      helpText={translate('UsenetDelayHelpText')}
                      onChange={onInputChange}
                    />
                  </FormGroup> :
                  null
              }

              {
                enableTorrent.value ?
                  <FormGroup>
                    <FormLabel>
                      {translate('TorrentDelay')}
                    </FormLabel>

                    <FormInputGroup
                      type={inputTypes.NUMBER}
                      name="torrentDelay"
                      unit="minutes"
                      {...torrentDelay}
                      helpText={translate('TorrentDelayHelpText')}
                      onChange={onInputChange}
                    />
                  </FormGroup> :
                  null
              }

              {
                enableDirectDownload.value ?
                  <FormGroup>
                    <FormLabel>
                      {translate('DirectDownloadDelay')}
                    </FormLabel>

                    <FormInputGroup
                      type={inputTypes.NUMBER}
                      name="directDownloadDelay"
                      unit="minutes"
                      {...directDownloadDelay}
                      helpText={translate('DirectDownloadDelayHelpText')}
                      onChange={onInputChange}
                    />
                  </FormGroup> :
                  null
              }

              <FormGroup>
                <FormLabel>{translate('BypassIfHighestQuality')}</FormLabel>

                <FormInputGroup
                  type={inputTypes.CHECK}
                  name="bypassIfHighestQuality"
                  {...bypassIfHighestQuality}
                  helpText={translate('BypassIfHighestQualityHelpText')}
                  onChange={onInputChange}
                />
              </FormGroup>

              <FormGroup>
                <FormLabel>{translate('BypassIfAboveCustomFormatScore')}</FormLabel>

                <FormInputGroup
                  type={inputTypes.CHECK}
                  name="bypassIfAboveCustomFormatScore"
                  {...bypassIfAboveCustomFormatScore}
                  helpText={translate('BypassIfAboveCustomFormatScoreHelpText')}
                  onChange={onInputChange}
                />
              </FormGroup>

              {
                bypassIfAboveCustomFormatScore.value ?
                  <FormGroup>
                    <FormLabel>{translate('MinimumCustomFormatScore')}</FormLabel>

                    <FormInputGroup
                      type={inputTypes.NUMBER}
                      name="minimumCustomFormatScore"
                      {...minimumCustomFormatScore}
                      helpText={translate('MinimumCustomFormatScoreHelpText')}
                      onChange={onInputChange}
                    />
                  </FormGroup> :
                  null
              }

              {
                id === 1 ?
                  <Alert>
                    {translate('DelayProfileDefaultNote')}
                  </Alert> :

                  <FormGroup>
                    <FormLabel>
                      {translate('Tags')}
                    </FormLabel>

                    <FormInputGroup
                      type={inputTypes.TAG}
                      name="tags"
                      {...tags}
                      helpText={translate('TagsHelpText')}
                      onChange={onInputChange}
                    />
                  </FormGroup>
              }
            </Form>
        }
      </ModalBody>
      <ModalFooter>
        {
          id && id > 1 &&
            <Button
              className={styles.deleteButton}
              kind={kinds.DANGER}
              onPress={onDeleteDelayProfilePress}
            >
              {translate('Delete')}
            </Button>
        }

        <Button
          onPress={onModalClose}
        >
          {translate('Cancel')}
        </Button>

        <SpinnerErrorButton
          isSpinning={isSaving}
          error={saveError}
          onPress={onSavePress}
        >
          {translate('Save')}
        </SpinnerErrorButton>
      </ModalFooter>
    </ModalContent>
  );
}

const delayProfileShape = {
  enableUsenet: PropTypes.shape(boolSettingShape).isRequired,
  enableTorrent: PropTypes.shape(boolSettingShape).isRequired,
  enableDirectDownload: PropTypes.shape(boolSettingShape).isRequired,
  preferredProtocol: PropTypes.object.isRequired,
  usenetDelay: PropTypes.shape(numberSettingShape).isRequired,
  torrentDelay: PropTypes.shape(numberSettingShape).isRequired,
  directDownloadDelay: PropTypes.shape(numberSettingShape).isRequired,
  bypassIfHighestQuality: PropTypes.shape(boolSettingShape).isRequired,
  bypassIfAboveCustomFormatScore: PropTypes.shape(boolSettingShape).isRequired,
  minimumCustomFormatScore: PropTypes.shape(numberSettingShape).isRequired,
  order: PropTypes.shape(numberSettingShape),
  tags: PropTypes.shape(tagSettingShape).isRequired
};

EditDelayProfileModalContent.propTypes = {
  id: PropTypes.number,
  isFetching: PropTypes.bool.isRequired,
  error: PropTypes.object,
  isSaving: PropTypes.bool.isRequired,
  saveError: PropTypes.object,
  item: PropTypes.shape(delayProfileShape).isRequired,
  enabledProtocols: PropTypes.arrayOf(PropTypes.string).isRequired,
  preferredProtocolOptions: PropTypes.arrayOf(PropTypes.object).isRequired,
  onInputChange: PropTypes.func.isRequired,
  onSavePress: PropTypes.func.isRequired,
  onModalClose: PropTypes.func.isRequired,
  onDeleteDelayProfilePress: PropTypes.func
};

export default EditDelayProfileModalContent;
