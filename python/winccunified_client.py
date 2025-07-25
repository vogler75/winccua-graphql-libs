# Python client for WinCC Unified GraphQL API
# Equivalent to winccunified-node.js
# Version: 1.0.0

import asyncio
import json
import logging
from typing import Dict, List, Optional, Callable, Any
import threading
import time

import aiohttp
from winccunified_graphql import QUERIES, MUTATIONS, SUBSCRIPTIONS

logger = logging.getLogger(__name__)


class GraphQLWSClient:
    """WebSocket client for GraphQL subscriptions"""
    
    def __init__(self, url: str, token: Optional[str] = None):
        self.url = url
        self.token = token
        self.websocket = None
        self.session = None
        self.subscriptions = {}
        self.subscription_id_counter = 0
        self.connection_state = 'disconnected'  # disconnected, connecting, connected
        self.connection_future = None
        self.keep_alive_task = None
        self.loop = None
        
    def generate_subscription_id(self) -> str:
        self.subscription_id_counter += 1
        return f"sub_{self.subscription_id_counter}"
    
    async def connect(self):
        """Connect to GraphQL WebSocket server"""
        if self.connection_state == 'connected':
            return
        
        if self.connection_state == 'connecting':
            return await self.connection_future
        
        self.connection_state = 'connecting'
        self.connection_future = asyncio.create_task(self._connect_impl())
        
        try:
            await self.connection_future
        finally:
            self.connection_future = None
    
    async def _connect_impl(self):
        """Internal connection implementation"""
        try:
            # Setup headers for WebSocket connection
            headers = {}
            if self.token:
                headers['Authorization'] = f'Bearer {self.token}'
            
            # Create session if needed
            if not self.session:
                self.session = aiohttp.ClientSession()
            
            # Connect to WebSocket using aiohttp
            self.websocket = await self.session.ws_connect(
                self.url,
                protocols=['graphql-transport-ws'],
                headers=headers
            )
            
            logger.info(f"[GraphQL-WS] WebSocket connected to {self.url}")
            
            # Send connection init message
            await self.websocket.send_str(json.dumps({
                'type': 'connection_init',
                'payload': {
                    'Authorization': f'Bearer {self.token}' if self.token else None,
                    'Content-Type': 'application/json'
                }
            }))
            
            # Wait for connection_ack
            await self._wait_for_connection_ack()
            
            # Start message handler
            asyncio.create_task(self._message_handler())
            
            self.connection_state = 'connected'
            logger.info("[GraphQL-WS] Connection established")
            
        except Exception as e:
            self.connection_state = 'disconnected'
            logger.error(f"[GraphQL-WS] Connection failed: {e}")
            raise
    
    async def _wait_for_connection_ack(self):
        """Wait for connection acknowledgment"""
        timeout = 10.0
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            try:
                message = await asyncio.wait_for(self.websocket.receive(), timeout=1.0)
                if message.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(message.data)
                    
                    if data.get('type') == 'connection_ack':
                        logger.info("[GraphQL-WS] Connection acknowledged")
                        return
                    elif data.get('type') == 'connection_error':
                        raise Exception(f"Connection error: {data.get('payload')}")
                elif message.type == aiohttp.WSMsgType.ERROR:
                    raise Exception(f"WebSocket error: {message.data}")
                    
            except asyncio.TimeoutError:
                continue
        
        raise Exception("Connection acknowledgment timeout")
    
    async def _message_handler(self):
        """Handle incoming WebSocket messages"""
        try:
            async for message in self.websocket:
                if message.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(message.data)
                    message_type = data.get('type')
                    
                    if message_type == 'next':
                        await self._handle_data_message(data)
                    elif message_type == 'error':
                        await self._handle_error_message(data)
                    elif message_type == 'complete':
                        await self._handle_complete_message(data)
                    elif message_type == 'pong':
                        logger.debug("[GraphQL-WS] Keep alive received")
                    else:
                        logger.warning(f"[GraphQL-WS] Unknown message type: {message_type}")
                elif message.type == aiohttp.WSMsgType.ERROR:
                    logger.error(f"[GraphQL-WS] WebSocket error: {message.data}")
                    break
                elif message.type == aiohttp.WSMsgType.CLOSE:
                    logger.info("[GraphQL-WS] WebSocket connection closed")
                    break
                    
        except Exception as e:
            logger.error(f"[GraphQL-WS] Message handler error: {e}")
        finally:
            self.connection_state = 'disconnected'
            await self._notify_subscriptions_disconnected()
    
    async def _handle_data_message(self, message: Dict):
        """Handle data messages from subscriptions"""
        subscription_id = message.get('id')
        payload = message.get('payload', {})
        
        if subscription_id in self.subscriptions:
            callback = self.subscriptions[subscription_id].get('on_data')
            if callback:
                try:
                    logger.debug(f"[GraphQL-WS] Subscription payload type: {type(payload)}")
                    logger.debug(f"[GraphQL-WS] Subscription payload content: {payload}")
                    await callback(payload)
                except Exception as e:
                    logger.error(f"Error in subscription callback: {e}")
                    logger.error(f"Payload type: {type(payload)}")
                    logger.error(f"Payload content: {payload}")
    
    async def _handle_error_message(self, message: Dict):
        """Handle error messages from subscriptions"""
        subscription_id = message.get('id')
        payload = message.get('payload', {})
        
        if subscription_id in self.subscriptions:
            callback = self.subscriptions[subscription_id].get('on_error')
            if callback:
                try:
                    await callback(Exception(str(payload)))
                except Exception as e:
                    logger.error(f"Error in subscription error callback: {e}")
    
    async def _handle_complete_message(self, message: Dict):
        """Handle complete messages from subscriptions"""
        subscription_id = message.get('id')
        
        if subscription_id in self.subscriptions:
            callback = self.subscriptions[subscription_id].get('on_complete')
            if callback:
                try:
                    await callback()
                except Exception as e:
                    logger.error(f"Error in subscription complete callback: {e}")
            
            # Remove subscription
            del self.subscriptions[subscription_id]
    
    async def _notify_subscriptions_disconnected(self):
        """Notify all subscriptions that connection was lost"""
        for subscription_id, subscription in self.subscriptions.items():
            callback = subscription.get('on_error')
            if callback:
                try:
                    await callback(Exception("WebSocket connection closed"))
                except Exception as e:
                    logger.error(f"Error in subscription disconnection callback: {e}")
    
    async def subscribe(self, query: str, variables: Dict = None, callbacks: Dict = None):
        """Subscribe to GraphQL subscription"""
        await self.connect()
        
        subscription_id = self.generate_subscription_id()
        
        # Store subscription callbacks
        self.subscriptions[subscription_id] = {
            'on_data': callbacks.get('on_data') if callbacks else None,
            'on_error': callbacks.get('on_error') if callbacks else None,
            'on_complete': callbacks.get('on_complete') if callbacks else None
        }
        
        # Send subscription message
        start_message = {
            'id': subscription_id,
            'type': 'subscribe',
            'payload': {
                'query': query,
                'variables': variables or {}
            }
        }
        
        await self.websocket.send_str(json.dumps(start_message))
        logger.info(f"[GraphQL-WS] Subscription started: {subscription_id}")
        
        # Return subscription object
        return {
            'id': subscription_id,
            'unsubscribe': lambda: asyncio.create_task(self.unsubscribe(subscription_id))
        }
    
    async def unsubscribe(self, subscription_id: str):
        """Unsubscribe from a subscription"""
        if subscription_id in self.subscriptions:
            # Send complete message
            if self.websocket and not self.websocket.closed:
                await self.websocket.send_str(json.dumps({
                    'id': subscription_id,
                    'type': 'complete'
                }))
            
            # Remove subscription
            del self.subscriptions[subscription_id]
            logger.info(f"[GraphQL-WS] Subscription stopped: {subscription_id}")
    
    async def disconnect(self):
        """Disconnect from WebSocket"""
        if self.keep_alive_task:
            self.keep_alive_task.cancel()
        
        # Stop all subscriptions
        for subscription_id in list(self.subscriptions.keys()):
            await self.unsubscribe(subscription_id)
        
        # Close WebSocket
        if self.websocket:
            await self.websocket.close()
            self.websocket = None
        
        # Close session
        if self.session:
            await self.session.close()
            self.session = None
        
        self.connection_state = 'disconnected'
    
    def update_token(self, token: str):
        """Update authentication token"""
        self.token = token
        # If connected and token is not None, need to reconnect with new token
        if self.connection_state == 'connected' and token is not None:
            logger.info("[GraphQL-WS] Token updated, reconnecting...")
            asyncio.create_task(self._reconnect_with_new_token())
    
    async def _reconnect_with_new_token(self):
        """Reconnect with new token"""
        await self.disconnect()
        await self.connect()


class GraphQLClient:
    """HTTP and WebSocket GraphQL client"""
    
    def __init__(self, http_url: str, ws_url: str):
        self.http_url = http_url
        self.ws_url = ws_url
        self.token = None
        self.ws_client = None
        self.session = None
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
        if self.ws_client:
            await self.ws_client.disconnect()
    
    def set_token(self, token: str):
        """Set authentication token"""
        self.token = token
        if self.ws_client:
            self.ws_client.update_token(token)
    
    def get_websocket_client(self) -> GraphQLWSClient:
        """Get WebSocket client for subscriptions"""
        if not self.ws_client:
            self.ws_client = GraphQLWSClient(self.ws_url, self.token)
        return self.ws_client
    
    async def request(self, query: str, variables: Dict = None) -> Dict:
        """Make HTTP GraphQL request"""
        if not self.session:
            self.session = aiohttp.ClientSession()
        
        headers = {
            'Content-Type': 'application/json'
        }
        
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        
        payload = {
            'query': query,
            'variables': variables or {}
        }
        
        async with self.session.post(self.http_url, json=payload, headers=headers) as response:
            if response.status != 200:
                raise Exception(f"HTTP error! status: {response.status}")
            
            result = await response.json()
            
            if 'errors' in result:
                error_messages = [e.get('message', str(e)) for e in result['errors']]
                raise Exception(f"GraphQL error: {', '.join(error_messages)}")
            
            return result.get('data', {})
    
    async def subscribe(self, query: str, variables: Dict = None, callbacks: Dict = None):
        """Subscribe to GraphQL subscription"""
        ws_client = self.get_websocket_client()
        return await ws_client.subscribe(query, variables, callbacks)


class WinCCUnifiedClient:
    """Main WinCC Unified client class"""
    
    def __init__(self, http_url: str, ws_url: str):
        self.client = GraphQLClient(http_url, ws_url)
        self.token = None
    
    async def __aenter__(self):
        await self.client.__aenter__()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.client.__aexit__(exc_type, exc_val, exc_tb)
    
    async def login(self, username: str, password: str) -> Dict:
        """
        Logs a user in based on their username and password.
        Returns: Session object containing user info, token, and expiry timestamp
        JSON Structure: { user: { id, name, groups, fullName, language, autoLogoffSec }, token: string, expires: timestamp, error?: { code, description } }
        Errors: 101 - Incorrect credentials provided, 102 - UMC error
        """
        result = await self.client.request(MUTATIONS['LOGIN'], {
            'username': username,
            'password': password
        })
        
        if result.get('login') and result['login'].get('token'):
            self.token = result['login']['token']
            self.client.set_token(self.token)
            return result['login']
        
        error_msg = result.get('login', {}).get('error', {}).get('description', 'Unknown error')
        raise Exception(f"Login failed: {error_msg}")
    
    def set_token(self, token: str):
        """Set authentication token"""
        self.token = token
        self.client.set_token(token)
    
    async def get_session(self, all_sessions: bool = False) -> Dict:
        """
        Returns information about the current session. If allSessions is true, returns all sessions of the current user.
        Returns: Array of Session objects with user info, token, and expiry timestamp
        JSON Structure: [{ user: { id, name, groups, fullName, language, autoLogoffSec }, token: string, expires: timestamp, error?: { code, description } }]
        """
        result = await self.client.request(QUERIES['SESSION'], {'allSessions': all_sessions})
        return result.get('session')
    
    async def get_tag_values(self, names: List[str], direct_read: bool = False) -> List[Dict]:
        """
        Queries tag values based on the provided names list. If directRead is true, values are taken directly from PLC.
        Returns: Array of TagValueResult objects with tag name, value, and quality information
        JSON Structure: [{ name: string, value: { value: variant, timestamp: timestamp, quality: { quality, subStatus, limit, extendedSubStatus, sourceQuality, sourceTime, timeCorrected } }, error?: { code, description } }]
        Errors: 2 - Cannot resolve provided name, 202 - Only leaf elements of a Structure Tag can be addressed
        """
        result = await self.client.request(QUERIES['TAG_VALUES'], {
            'names': names,
            'directRead': direct_read
        })
        return result.get('tagValues', [])
    
    async def get_logged_tag_values(self, names: List[str], start_time: Optional[str] = None,
                                   end_time: Optional[str] = None, max_number_of_values: int = 1000,
                                   sorting_mode: str = 'TIME_ASC') -> List[Dict]:
        """
        Queries logged tag values from the database. Names must be LoggingTag names or Tag names (if only one logging tag exists).
        Returns: Array of LoggedTagValuesResult objects with logging tag name, error info, and array of logged values
        JSON Structure: [{ loggingTagName: string, error?: { code, description }, values: [{ value: { value: variant, timestamp: timestamp, quality: quality }, flags: [flag_enum] }] }]
        Sorting modes: TIME_ASC, TIME_DESC. Bounding modes: NO_BOUNDING_VALUES, LEFT_BOUNDING_VALUES, RIGHT_BOUNDING_VALUES, LEFTRIGHT_BOUNDING_VALUES
        Errors: 1 - Generic error, 2 - Cannot resolve provided name, 3 - Argument error
        """
        variables = {
            'names': names,
            'maxNumberOfValues': max_number_of_values,
            'sortingMode': sorting_mode
        }
        
        if start_time:
            variables['startTime'] = start_time
        if end_time:
            variables['endTime'] = end_time
        
        result = await self.client.request(QUERIES['LOGGED_TAG_VALUES'], variables)
        return result.get('loggedTagValues', [])
    
    async def get_nonce(self) -> Dict:
        """
        Returns a nonce that can be used with e.g. the UMC SWAC login method.
        Returns: Nonce object with value and validity duration
        JSON Structure: { value: string, validFor: number }
        """
        result = await self.client.request(QUERIES['NONCE'])
        return result.get('nonce')
    
    async def get_identity_provider_url(self) -> str:
        """
        Returns the URL of the identity provider for UMC SWAC authentication.
        Returns: String URL where user should be redirected for SWAC login
        JSON Structure: string (URL)
        """
        result = await self.client.request(QUERIES['IDENTITY_PROVIDER_URL'])
        return result.get('identityProviderURL')
    
    async def browse(self, name_filters: List[str] = None, object_type_filters: List[str] = None,
                    base_type_filters: List[str] = None, language: str = 'en-US') -> List[Dict]:
        """
        Queries tags, elements, types, alarms, logging tags based on filter criteria. Each filter parameter supports arrays with OR relation, while parameters have AND relation.
        Returns: Array of BrowseTagsResult objects with name, display name, object type, and data type
        JSON Structure: [{ name: string, displayName: string, objectType: string, dataType: string }]
        ObjectTypes: TAG, SIMPLETAG, STRUCTURETAG, TAGTYPE, STRUCTURETAGTYPE, SIMPLETAGTYPE, ALARM, ALARMCLASS, LOGGINGTAG
        Errors: 1 - Generic error, 2 - Cannot resolve provided name, 3 - Argument error
        """
        result = await self.client.request(QUERIES['BROWSE'], {
            'nameFilters': name_filters or [],
            'objectTypeFilters': object_type_filters or [],
            'baseTypeFilters': base_type_filters or [],
            'language': language
        })
        return result.get('browse', [])
    
    async def get_active_alarms(self, system_names: List[str] = None, filter_string: str = '',
                               filter_language: str = 'en-US', languages: List[str] = None) -> List[Dict]:
        """
        Query active alarms from the provided systems using ChromQueryLanguage filter.
        Returns: Array of ActiveAlarm objects with comprehensive alarm information
        JSON Structure: [{ name: string, instanceID: number, alarmGroupID: number, raiseTime: timestamp, acknowledgmentTime: timestamp, clearTime: timestamp, resetTime: timestamp, modificationTime: timestamp, state: AlarmState, textColor: color, backColor: color, flashing: boolean, languages: [string], alarmClassName: string, alarmClassSymbol: [string], alarmClassID: number, stateMachine: AlarmStateMachine, priority: number, alarmParameterValues: [variant], alarmType: [string], eventText: [string], infoText: [string], alarmText1-9: [string], stateText: [string], origin: string, area: string, changeReason: [AlarmChangeReason], connectionName: string, valueLimit: variant, sourceType: AlarmSourceType, suppressionState: AlarmSuppressionState, hostName: string, userName: string, value: variant, valueQuality: Quality, quality: Quality, invalidFlags: AlarmInvalidFlags, deadBand: variant, producer: AlarmProducer, duration: timespan, durationIso: timespanIso, sourceID: string, systemSeverity: number, loopInAlarm: string, loopInAlarmParameterValues: variant, path: string, userResponse: AlarmUserResponse }]
        Errors: 301 - Syntax error in query string, 302 - Invalid language, 303 - Invalid filter language
        """
        result = await self.client.request(QUERIES['ACTIVE_ALARMS'], {
            'systemNames': system_names or [],
            'filterString': filter_string,
            'filterLanguage': filter_language,
            'languages': languages or ['en-US']
        })
        return result.get('activeAlarms', [])
    
    async def get_logged_alarms(self, system_names: List[str] = None, filter_string: str = '',
                               filter_language: str = 'en-US', languages: List[str] = None,
                               start_time: Optional[str] = None, end_time: Optional[str] = None,
                               max_number_of_results: int = 0) -> List[Dict]:
        """
        Query logged alarms from the storage system using ChromQueryLanguage filter and time boundaries.
        Returns: Array of LoggedAlarm objects with comprehensive historical alarm information
        JSON Structure: [{ name: string, instanceID: number, alarmGroupID: number, raiseTime: timestamp, acknowledgmentTime: timestamp, clearTime: timestamp, resetTime: timestamp, modificationTime: timestamp, state: AlarmState, textColor: color, backColor: color, languages: [string], alarmClassName: string, alarmClassSymbol: [string], alarmClassID: number, stateMachine: AlarmStateMachine, priority: number, alarmParameterValues: [variant], alarmType: [string], eventText: [string], infoText: [string], alarmText1-9: [string], stateText: [string], origin: string, area: string, changeReason: [AlarmChangeReason], valueLimit: variant, sourceType: AlarmSourceType, suppressionState: AlarmSuppressionState, hostName: string, userName: string, value: variant, valueQuality: Quality, quality: Quality, invalidFlags: AlarmInvalidFlags, deadband: variant, producer: AlarmProducer, duration: timespan, durationIso: timespanIso, hasComments: boolean }]
        Errors: 301 - Syntax error in query string, 302 - Invalid language (or not logged), 303 - Invalid filter language (or not logged)
        """
        variables = {
            'systemNames': system_names or [],
            'filterString': filter_string,
            'filterLanguage': filter_language,
            'languages': languages or ['en-US'],
            'maxNumberOfResults': max_number_of_results
        }
        
        if start_time:
            variables['startTime'] = start_time
        if end_time:
            variables['endTime'] = end_time
        
        result = await self.client.request(QUERIES['LOGGED_ALARMS'], variables)
        return result.get('loggedAlarms', [])
    
    async def login_swac(self, claim: str, signed_claim: str) -> Dict:
        """
        Logs a user in based on the claim and signed claim from UMC SWAC authentication.
        Returns: Session object containing user info, token, and expiry timestamp
        JSON Structure: { user: { id, name, groups, fullName, language, autoLogoffSec }, token: string, expires: timestamp, error?: { code, description } }
        Errors: 101 - Incorrect credentials provided, 103 - Nonce expired
        """
        result = await self.client.request(MUTATIONS['LOGIN_SWAC'], {
            'claim': claim,
            'signedClaim': signed_claim
        })
        
        if result.get('loginSWAC') and result['loginSWAC'].get('token'):
            self.token = result['loginSWAC']['token']
            self.client.set_token(self.token)
            return result['loginSWAC']
        
        error_msg = result.get('loginSWAC', {}).get('error', {}).get('description', 'Unknown error')
        raise Exception(f"SWAC login failed: {error_msg}")
    
    async def extend_session(self) -> Dict:
        """
        Extends the user's current session expiry by the 'session expires' value from the identity provider (UMC).
        Returns: Session object with updated expiry timestamp
        JSON Structure: { user: { id, name, groups, fullName, language, autoLogoffSec }, token: string, expires: timestamp, error?: { code, description } }
        """
        result = await self.client.request(MUTATIONS['EXTEND_SESSION'])
        
        if result.get('extendSession') and result['extendSession'].get('token'):
            self.token = result['extendSession']['token']
            self.client.set_token(self.token)
            return result['extendSession']
        
        error_msg = result.get('extendSession', {}).get('error', {}).get('description', 'Unknown error')
        raise Exception(f"Session extension failed: {error_msg}")
    
    async def logout(self, all_sessions: bool = False) -> bool:
        """
        Logs out the current user. If allSessions is true, all sessions of the current user will be terminated.
        Returns: Boolean indicating success
        JSON Structure: boolean
        """
        # First disconnect WebSocket client if exists
        if self.client.ws_client:
            await self.client.ws_client.disconnect()
        
        result = await self.client.request(MUTATIONS['LOGOUT'], {'allSessions': all_sessions})
        self.token = None
        self.client.set_token(None)
        return result.get('logout', False)
    
    async def write_tag_values(self, tag_values: List[Dict], timestamp: Optional[str] = None,
                              quality: Optional[Dict] = None) -> List[Dict]:
        """
        Updates tags based on the provided TagValueInput list. Uses fallback timestamp and quality if not specified per tag.
        Returns: Array of WriteTagValuesResult objects with tag name and error information
        JSON Structure: [{ name: string, error?: { code, description } }]
        Errors: 2 - Cannot resolve provided name, 201 - Cannot convert provided value to data type, 202 - Only leaf elements of a Structure Tag can be addressed
        """
        variables = {'input': tag_values}
        if timestamp:
            variables['timestamp'] = timestamp
        if quality:
            variables['quality'] = quality
        
        result = await self.client.request(MUTATIONS['WRITE_TAG_VALUES'], variables)
        return result.get('writeTagValues', [])
    
    async def acknowledge_alarms(self, alarm_identifiers: List[Dict]) -> List[Dict]:
        """
        Acknowledge one or more alarms. Each alarm identifier must have the alarm name and optionally an instanceID.
        Returns: Array of ActiveAlarmMutationResult objects with alarm name, instance ID, and error information
        JSON Structure: [{ alarmName: string, alarmInstanceID: number, error?: { code, description } }]
        Errors: 2 - Cannot resolve provided name, 304 - Invalid object state, 305 - Alarm cannot be acknowledged in current state
        """
        result = await self.client.request(MUTATIONS['ACKNOWLEDGE_ALARMS'], {
            'input': alarm_identifiers
        })
        return result.get('acknowledgeAlarms', [])
    
    async def reset_alarms(self, alarm_identifiers: List[Dict]) -> List[Dict]:
        """
        Reset one or more alarms. Each alarm identifier must have the alarm name and optionally an instanceID.
        Returns: Array of ActiveAlarmMutationResult objects with alarm name, instance ID, and error information
        JSON Structure: [{ alarmName: string, alarmInstanceID: number, error?: { code, description } }]
        Errors: 2 - Cannot resolve provided name, 304 - Invalid object state, 305 - Alarm cannot be reset in current state
        """
        result = await self.client.request(MUTATIONS['RESET_ALARMS'], {
            'input': alarm_identifiers
        })
        return result.get('resetAlarms', [])
    
    async def disable_alarms(self, names: List[str]) -> List[Dict]:
        """
        Disable the creation of new alarm instances for one or more alarms.
        Returns: Array of AlarmMutationResult objects with alarm name and error information
        JSON Structure: [{ alarmName: string, error?: { code, description } }]
        Errors: 2 - Cannot resolve provided name
        """
        result = await self.client.request(MUTATIONS['DISABLE_ALARMS'], {'names': names})
        return result.get('disableAlarms', [])
    
    async def enable_alarms(self, names: List[str]) -> List[Dict]:
        """
        Enable the creation of new alarm instances for one or more alarms.
        Returns: Array of AlarmMutationResult objects with alarm name and error information
        JSON Structure: [{ alarmName: string, error?: { code, description } }]
        Errors: 2 - Cannot resolve provided name
        """
        result = await self.client.request(MUTATIONS['ENABLE_ALARMS'], {'names': names})
        return result.get('enableAlarms', [])
    
    async def shelve_alarms(self, names: List[str], shelve_timeout: Optional[str] = None) -> List[Dict]:
        """
        Shelve all active alarm instances of the provided configured alarms. Uses runtime's configured shelving timeout if not specified.
        Returns: Array of AlarmMutationResult objects with alarm name and error information
        JSON Structure: [{ alarmName: string, error?: { code, description } }]
        Errors: 2 - Cannot resolve provided name
        """
        variables = {'names': names}
        if shelve_timeout:
            variables['shelveTimeout'] = shelve_timeout
        
        result = await self.client.request(MUTATIONS['SHELVE_ALARMS'], variables)
        return result.get('shelveAlarms', [])
    
    async def unshelve_alarms(self, names: List[str]) -> List[Dict]:
        """
        Revert the Shelve action for the provided configured alarms. Unshelving causes a notification for all concerned alarm instances.
        Returns: Array of AlarmMutationResult objects with alarm name and error information
        JSON Structure: [{ alarmName: string, error?: { code, description } }]
        Errors: 2 - Cannot resolve provided name
        """
        result = await self.client.request(MUTATIONS['UNSHELVE_ALARMS'], {'names': names})
        return result.get('unshelveAlarms', [])
    
    async def subscribe_to_tag_values(self, names: List[str], on_data: Callable = None,
                                     on_error: Callable = None, on_complete: Callable = None):
        """
        Subscribes to tag values for the tags based on the provided names list. Notifications contain reason (Added, Modified, Removed, Removed (Name changed)).
        Returns: Subscription object with unsubscribe method
        Callback receives: TagValueNotification object { name: string, value: { value: variant, timestamp: timestamp, quality: Quality }, error?: { code, description }, notificationReason: string }
        Errors: 2 - Cannot resolve provided name, 202 - Only leaf elements of a Structure Tag can be addressed
        """
        callbacks = {}
        if on_data:
            callbacks['on_data'] = on_data
        if on_error:
            callbacks['on_error'] = on_error
        if on_complete:
            callbacks['on_complete'] = on_complete
        
        return await self.client.subscribe(
            SUBSCRIPTIONS['TAG_VALUES'],
            {'names': names},
            callbacks
        )
    
    async def subscribe_to_active_alarms(self, system_names: List[str] = None, filter_string: str = '',
                                        filter_language: str = 'en-US', languages: List[str] = None,
                                        on_data: Callable = None, on_error: Callable = None,
                                        on_complete: Callable = None):
        """
        Subscribe for active alarms matching the given filters. Notifications contain reason (Added, Modified, Removed).
        Returns: Subscription object with unsubscribe method
        Callback receives: ActiveAlarmNotification object with all ActiveAlarm fields plus notificationReason: string
        Errors: 301 - Syntax error in query string, 302 - Invalid language, 303 - Invalid filter language
        """
        callbacks = {}
        if on_data:
            callbacks['on_data'] = on_data
        if on_error:
            callbacks['on_error'] = on_error
        if on_complete:
            callbacks['on_complete'] = on_complete
        
        return await self.client.subscribe(
            SUBSCRIPTIONS['ACTIVE_ALARMS'],
            {
                'systemNames': system_names or [],
                'filterString': filter_string,
                'filterLanguage': filter_language,
                'languages': languages or ['en-US']
            },
            callbacks
        )
    
    async def subscribe_to_redu_state(self, on_data: Callable = None, on_error: Callable = None,
                                     on_complete: Callable = None):
        """
        Subscribes to redu state. Notifications contain information about the active/passive state of the system on state changes.
        Returns: Subscription object with unsubscribe method
        Callback receives: ReduStateNotification object { value: { value: ReduState (ACTIVE | PASSIVE), timestamp: timestamp }, notificationReason: string }
        """
        callbacks = {}
        if on_data:
            callbacks['on_data'] = on_data
        if on_error:
            callbacks['on_error'] = on_error
        if on_complete:
            callbacks['on_complete'] = on_complete
        
        return await self.client.subscribe(
            SUBSCRIPTIONS['REDU_STATE'],
            {},
            callbacks
        )