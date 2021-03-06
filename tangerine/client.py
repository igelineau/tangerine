from urllib.parse import urlencode, quote
from . import exceptions
from .login import TangerineLoginFlow
import functools
import datetime
import contextlib
import requests
import logging
import json


logger = logging.getLogger(__name__)


DEFAULT_LOCALE = 'en_CA'


def api_response(root_key='', check_response_status=True):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            response_json = fn(*args, **kwargs)
            if check_response_status and response_json['response_status']['status_code'] != 'SUCCESS':
                raise exceptions.APIResponseError(response_json)
            if not root_key:
                return response_json
            return response_json[root_key]
        return wrapper
    return decorator


class TangerineClient(object):
    def __init__(self, secret_provider, session=None, locale=DEFAULT_LOCALE):
        if session is None:
            session = requests.Session()
        self.session = session
        self.login_flow = TangerineLoginFlow(secret_provider, self.session, locale)

    def _api_get(self, path):
        url = 'https://secure.tangerine.ca/web/rest{}'.format(path)
        response = self.session.get(url)
        response.raise_for_status()
        return response.json()

    def _api_post(self, path, data):

        transaction_token = self.session.cookies.get('TRANSACTION_TOKEN')
        resp = self.session.post(
            'https://secure.tangerine.ca/web/rest{}'.format(path),
            headers={
                'x-web-flavour': 'fbe',
                'Accept': 'application/json',
                'Origin': 'https://www.tangerine.ca',
                'Referrer': 'https://www.tangerine.ca/app/',
                'User-Agent': 'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:57.0) Gecko/20100101 Firefox/57.0',
                'x-transaction-token': transaction_token
            },
            json=data)
        resp.raise_for_status()
        logger.debug(resp.text)
        return resp

    @contextlib.contextmanager
    def login(self):
        self.login_flow.start()
        try:
            yield
        except Exception:
            raise
        finally:
            self.login_flow.end()

    @api_response('customer')
    def me(self):
        return self._api_get('/v1/customers/my')

    @api_response('accounts')
    def list_accounts(self):
        return self._api_get('/pfm/v1/accounts')

    @api_response('account_summary')
    def get_account(self, account_id: str):
        return self._api_get('/v1/accounts/{}?billing-cycle-ranges=true'.format(account_id))

    @api_response('transactions')
    def list_transactions(self, account_ids: list, period_from: datetime.date, period_to: datetime.date):
        params = {
            'accountIdentifiers': ','.join(account_ids),
            'hideAuthorizedStatus': True,
            'periodFrom': period_from.strftime('%Y-%m-%dT00:00:00.000Z'),
            'periodTo': period_to.strftime('%Y-%m-%dT00:00:00.000Z'),
            'skip': 0,
        }
        return self._api_get('/pfm/v1/transactions?{}'.format(urlencode(params)))

    @api_response('pending_transactions')
    def list_pending_transactions(self):
        params = {
            'include-mf-transactions': 'true',
        }
        return self._api_get('/v1/customers/my/pending-transactions?{}'.format(urlencode(params)))

    @api_response('recipient')
    def list_email_recipients(self):
        return self._api_get('/v1/customers/my/emt-recipients')

    @api_response()
    def list_move_money_accounts(self):
        params = {
            'actionCode': 'MOVE_MONEY'
        }
        return self._api_get('/v1/customers/my/accounts?{}'.format(urlencode(params)))

    @api_response('token', check_response_status=False)
    def _get_transaction_download_token(self):
        return self._api_get('/v1/customers/my/security/transaction-download-token')

    def download_ofx(self, account, start_date: datetime.date, end_date: datetime.date, save=True):
        """
        Download OFX file.

        :param account: The id of the account
        :param start_date: The start date of the statement period
        :param end_date: The end date of the statement period
        :param save: Save to a file. If False, the content of the OFX will be returned
        :return: The filename of the OFX file if save is True, otherwise the content of the OFX file
        """
        if account['type'] == 'CHEQUING':
            account_type = 'SAVINGS'
            account_display_name = account['display_name']
            account_nickname = account['nickname']
        elif account['type'] == 'SAVINGS':
            account_type = 'SAVINGS'
            account_display_name = account['display_name']
            account_nickname = account['nickname']
        elif account['type'] == 'CREDIT_CARD':
            account_type = 'CREDITLINE'
            account_details = self.get_account(account['number'])
            account_display_name = account_details['display_name']
            account_nickname = account_details['account_nick_name']
        else:
            raise exceptions.UnsupportedAccountTypeForDownload(account['type'])

        token = self._get_transaction_download_token()
        filename = '{}.QFX'.format(account_nickname)
        params = {
            'fileType': 'QFX',
            'ofxVersion': '102',
            'sessionId': 'tng',
            'orgName': 'Tangerine',
            'bankId': '0614',
            'language': 'eng',
            'acctType': account_type,
            'acctNum': account_display_name,
            'acctName': account_nickname,
            'userDefined': token,
            'startDate': start_date.strftime('%Y%m%d'),
            'endDate': end_date.strftime('%Y%m%d'),
            'orgId': 10951,
            'custom.tag': 'customValue',
            'csvheader': 'Date,Transaction,Name,Memo,Amount',
        }
        response = self.session.get('https://ofx.tangerine.ca/{}?{}'.format(quote(filename), urlencode(params)),
                                    headers={'Referer': 'https://www.tangerine.ca/app/'})
        response.raise_for_status()
        if save:
            local_filename = '{}_{}-{}.QFX'.format(account_nickname,
                                                   start_date.strftime('%Y%m%d'),
                                                   end_date.strftime('%Y%m%d'))
            with open(local_filename, 'w') as f:
                f.write(response.text)

            logger.info('Saved: {}'.format(local_filename))
            return local_filename
        else:
            return response.text

    @api_response()
    def move_money(self, account_id, from_account, to_account, amount: float, currency, when):
        path = '/v1/accounts/{}/transactions/movemoney'.format(account_id)
        data = {
            'transfers': {
                'when': when,
                'amount': amount,
                'to_account': to_account,
                'from_account': from_account,
                'currency': currency
            },
            'validate_only': False
        }
        print(data)

        response = self._api_post(path, data)
        print(response.json())

    @api_response()
    def email_money(self,
                    source_account_number,
                    recipient_sequence_number,
                    amount,
                    when,
                    scheduled_date: datetime.date,
                    emt_type='INTERAC_EMT'):
        path = '/v1/accounts/{}/transactions/emts'.format(source_account_number)
        data = {
            'validate_only': False,
            'emt': {
                'emt_type': emt_type,
                'amount': amount,
                'recipient_sequence_number': recipient_sequence_number,
                'message': '',
                'accepted_terms_and_conditions': True,
                'when': when,
                'scheduled_date': scheduled_date.strftime('%Y-%m-%d')
            }
        }

        response = self._api_post(path, data)
        print(response.json())
