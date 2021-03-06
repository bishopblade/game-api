# -*- coding: utf-8 -*-`
"""api.py - Create and configure the Game API exposing the resources.
This can also contain game logic. For more complex games it would be wise to
move game logic to another file. Ideally the API will be simple, concerned
primarily with communication to/from the API's users."""

import csv
import json
import endpoints
from protorpc import remote, messages
from google.appengine.api import memcache
from google.appengine.api import taskqueue
from google.appengine.ext import ndb

from models import (
    User,
    Game,
    Score,
    StringMessage,
    NewGameForm,
    UserGamesForm,
    GameForm,
    MakeMoveForm,
    ScoreForms,
    MessageForm,
    RankingsMessage,
    RankingMessage
)

from utils import get_by_urlsafe

WORDS_LIST = []
with open('countries.csv') as countries:
    reader = csv.reader(countries)
    for row in reader:
        WORDS_LIST.append(row[1])


NEW_GAME_REQUEST = endpoints.ResourceContainer(NewGameForm)
GET_USER_GAMES_REQUEST = endpoints.ResourceContainer(
        user=messages.StringField(1))
GET_GAME_REQUEST = endpoints.ResourceContainer(
        urlsafe_game_key=messages.StringField(1),)
MAKE_MOVE_REQUEST = endpoints.ResourceContainer(
    MakeMoveForm,
    urlsafe_game_key=messages.StringField(1),)
USER_REQUEST = endpoints.ResourceContainer(user_name=messages.StringField(1),
                                           email=messages.StringField(2))

NUMBER_RESULTS_REQUEST = endpoints.ResourceContainer(number_of_results=messages.IntegerField(1))

MEMCACHE_MOVES_REMAINING = 'MOVES_REMAINING'

def updateHistory(game, guess, msg):
    history = json.loads(game.history)
    history.append([guess, msg])
    game.history = json.dumps(history)
    game.put()

@endpoints.api(name='hangman', version='v1')
class HangmanApi(remote.Service):
    """Hangman API"""
    @endpoints.method(request_message=USER_REQUEST,
                      response_message=StringMessage,
                      path='user',
                      name='create_user',
                      http_method='POST')
    def create_user(self, request):
        """Create a User. Requires a unique username"""
        if User.query(User.name == request.user_name).get():
            raise endpoints.ConflictException(
                    'A User with that name already exists!')
        user = User(name=request.user_name, email=request.email)
        user.put()
        return StringMessage(message='User {} created!'.format(
                request.user_name))

    @endpoints.method(request_message=NEW_GAME_REQUEST,
                      response_message=GameForm,
                      path='game',
                      name='new_game',
                      http_method='POST')
    def new_game(self, request):
        """Creates new game"""
        user = User.query(User.name == request.user_name).get()
        if not user:
            raise endpoints.NotFoundException(
                    'A User with that name does not exist!')
        game = Game.new_game(user.key, request.attempts)

        # Use a task queue to update the average attempts remaining.
        # This operation is not needed to complete the creation of a new game
        # so it is performed out of sequence.
        taskqueue.add(url='/tasks/cache_average_attempts')
        return game.to_form('Have fun playing Hangman!')

    @endpoints.method(request_message=GET_USER_GAMES_REQUEST,
            response_message=UserGamesForm,
            path='user/{user}/games',
            name='get_user_games',
            http_method='GET')
    def get_user_games(self, request):
        """Returns all games for given user"""

        user = User.query(User.name == request.user).get()
        if not user:
            raise endpoints.NotFoundException(
                    'A user with that name does not exist!')
        games = Game.query(Game.user == user.key).fetch()
        game_keys = []
        for game in games:
            if not game.game_over:
                game_keys.append(game.key.urlsafe())

        return UserGamesForm(games=game_keys)

    @endpoints.method(request_message=GET_GAME_REQUEST,
            response_message=MessageForm,
            path='/game/{urlsafe_game_key}/cancel',
            name='cancel_game',
            http_method='DELETE')
    def cancel_game(self, request):
        """Cancel the game."""
        game = get_by_urlsafe(request.urlsafe_game_key, Game)
        if game:
            if game.game_over:
                raise endpoints.BadRequestException('Game is already over.')
            else:
                game.key.delete()
                return MessageForm(message='Game deleted.')
        else:
            raise endpoints.NotFoundException('Game not found!')


    @endpoints.method(request_message=GET_GAME_REQUEST,
                      response_message=GameForm,
                      path='game/{urlsafe_game_key}',
                      name='get_game',
                      http_method='GET')
    def get_game(self, request):
        """Return the current game state."""
        game = get_by_urlsafe(request.urlsafe_game_key, Game)
        if game:
            return game.to_form('Time to make a move!')
        else:
            raise endpoints.NotFoundException('Game not found!')

    @endpoints.method(request_message=GET_GAME_REQUEST,
            response_message=MessageForm,
            path='/game/{urlsafe_game_key}/history',
            name='get_game_history',
            http_method='GET')
    def get_game_history(self, request):
        """Return the game's history of moves."""
        game = get_by_urlsafe(request.urlsafe_game_key, Game)
        if game:
            return MessageForm(message=game.history)
        else:
            raise endpoints.NotFoundException('Game not found!')

    @endpoints.method(request_message=MAKE_MOVE_REQUEST,
                      response_message=GameForm,
                      path='game/{urlsafe_game_key}',
                      name='make_move',
                      http_method='PUT')
    def make_move(self, request):
        """Makes a move. Returns a game state with message"""
        game = get_by_urlsafe(request.urlsafe_game_key, Game)
        if game.game_over:
            return endpoints.BadRequestException('Game is already over!')

        letterInWord = False
        if request.guess.isalpha():
            if request.guess == game.target:
                msg = 'You win! Word was %s' % game.target
                updateHistory(game, request.guess, msg)
                game.end_game(True)
                return game.to_form(msg)
            else:
                if len(request.guess) == 1:
                    for i, c in enumerate(game.target):
                        if request.guess.upper() == c and i not in game.guessed_letters:
                            game.guessed_letters.append(i)
                            letterInWord = True
                else:
                    raise endpoints.BadRequestException('Guess must be a single character or the word!')
        else:
            raise endpoints.BadRequestException('Guess must be all letters!')

        if len(game.guessed_letters) == len(game.target):
            msg = 'You win! Word was %s' % game.word_progress()
            updateHistory(game, request.guess, msg)
            game.end_game(True)
            return game.to_form(msg)

        if letterInWord:
            msg = 'Letter was in the word! Word progress: %s'
        else:
            msg = 'Letter was not in the word! Word progress: %s'
            game.attempts_remaining -= 1

        if game.attempts_remaining < 1:
            msg = 'Game over!'
            updateHistory(game, request.guess, msg)
            game.end_game(False)
            return game.to_form(msg)
        else:
            updateHistory(game, request.guess, msg % game.word_progress())
            return game.to_form(msg % game.word_progress())

    @endpoints.method(response_message=ScoreForms,
                      path='scores',
                      name='get_scores',
                      http_method='GET')
    def get_scores(self, request):
        """Return all scores"""
        return ScoreForms(items=[score.to_form() for score in Score.query()])

    @endpoints.method(request_message=USER_REQUEST,
                      response_message=ScoreForms,
                      path='scores/user/{user_name}',
                      name='get_user_scores',
                      http_method='GET')
    def get_user_scores(self, request):
        """Returns all of an individual User's scores"""
        user = User.query(User.name == request.user_name).get()
        if not user:
            raise endpoints.NotFoundException(
                    'A User with that name does not exist!')
        scores = Score.query(Score.user == user.key)
        return ScoreForms(items=[score.to_form() for score in scores])

    @endpoints.method(request_message=NUMBER_RESULTS_REQUEST,
            response_message=ScoreForms,
            path='/scores/user/{user_name}/high',
            name='get_high_scores',
            http_method='GET')
    def get_high_scores(self, request):
        """Returns all of a User's scores sorted by score"""
        scores = Score.query().order(Score.guesses)
        return ScoreForms(items=[score.to_form() for score in scores.fetch(request.number_of_results)])

    @endpoints.method(response_message=RankingsMessage,
            path='/rankings',
            name='get_user_rankings',
            http_method='GET')
    def get_user_rankings(self, request):
        """Returns all users' rankings"""
        # Re-put all User entities to recalculate ranking points
        ndb.put_multi(User.query().fetch())
        users = User.query().order(User.ranking_points).fetch()
        return RankingsMessage(rankings=[user.to_form() for user in users])

    @endpoints.method(response_message=StringMessage,
                      path='games/average_attempts',
                      name='get_average_attempts_remaining',
                      http_method='GET')
    def get_average_attempts(self, request):
        """Get the cached average moves remaining"""
        return StringMessage(message=memcache.get(MEMCACHE_MOVES_REMAINING) or '')

    @staticmethod
    def _cache_average_attempts():
        """Populates memcache with the average moves remaining of Games"""
        games = Game.query(Game.game_over == False).fetch()
        if games:
            count = len(games)
            total_attempts_remaining = sum([game.attempts_remaining
                                        for game in games])
            average = float(total_attempts_remaining)/count
            memcache.set(MEMCACHE_MOVES_REMAINING,
                         'The average moves remaining is {:.2f}'.format(average))


api = endpoints.api_server([HangmanApi])
