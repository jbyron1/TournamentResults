import argparse
from gql import Client, gql, dsl
from gql.transport.requests import RequestsHTTPTransport
import re
import time


def gen_headers():
    try:
        with open('auth.txt', 'r') as auth:
            key = auth.read().strip()
            header = {"Content-Type": "application/json",
                      "Authorization": "Bearer " + key}
            return header
    except FileNotFoundError as e:
        "Could not open auth.txt, please put start.gg api key in auth.txt"


def execute(query, session, vars={}):
    sleepTime = 1
    while True:
        try:
            return session.execute(query, variable_values=vars)
        except:
            time.sleep(sleepTime)
            sleepTime = sleepTime * 2


def get_event_id(session, event_slug: str) -> int:
    """
    Get event id from a single event slug
    """
    q = gql("""
    query getEventID($slug: String) {
      event(slug: $slug){
        id
        tournament{
            name
        }
        videogame{
          name
        }
      }
    }
  """)
    params = {"slug": event_slug}

    result = execute(q, session, params)

    event_id = result['event']['id']
    game = result['event']['videogame']['name']
    tournament = result['event']['tournament']['name']
    return event_id, game, tournament


def getEvents(session, tournament_slug: str) -> dict:
    """
    Get a dictionary of event ids and event games from a tournament slug
    """
    q = gql("""
    query getEvents($slug: String) {
    tournament(slug: $slug) {
    events {
      id
      videogame {
        name
      }
      name
    }
    }
    }
    """)

    params = {"slug": tournament_slug}

    event_dict = {}

    result = execute(q, session, params)
    for event in result['tournament']['events']:
        event_dict[event['id']] = (event['videogame']['name'], event['name'])

    return event_dict


def getAllEventStandings(event_id: int, inner: dsl.DSLField, ds: dsl.DSLSchema, session, initialPerPage=100):
    """
    Get a complete list of standings for an event, inner contains the fields you want to get for each entrant
    """
    perPage = initialPerPage

    # startgg has been known to fail from time to time, limits the number of attempts so the program doesn't get stuck
    for i in range(5):
        nodes = []
        # get the number of pages necessary as well as total count of entrants
        query = dsl.dsl_gql(
            dsl.DSLQuery(
                ds.Query.event(id=event_id).select(ds.Event.standings(query={"page": 1, "perPage": perPage}).select(
                    ds.StandingConnection.pageInfo.select(ds.PageInfo.total, ds.PageInfo.totalPages)))
            )
        )
        result = execute(query, session)
        total = result['event']['standings']['pageInfo']['total']
        totalPages = result['event']['standings']['pageInfo']['totalPages']

        # For each page of entrants, collect each one and collate them into a single list of nodes
        try:
            for page in range(1, totalPages + 1):
                query = dsl.dsl_gql(
                    dsl.DSLQuery(
                        ds.Query.event(id=event_id).select(ds.Event.standings(query={
                            "page": page, "perPage": perPage}).select(ds.StandingConnection.nodes.select(*inner)))
                    )
                )
                # probably bad, but retry query until it gets a success.
                # TODO figure out more robust exception handling
                while True:
                    try:
                        result = execute(query, session)
                    except gql.transport.exceptions.TransportServerError as e:
                        time.sleep(4)
                        continue
                    break

                for node in result['event']['standings']['nodes']:
                    nodes.append(node)

        except Exception as e:
            print(e)
            perPage = perPage / 2

        if len(nodes) == total:
            return nodes
        else:
            print(len(nodes), total)

    print("failed to gather entrants")


def getEventResults(session, event_id: int, ds: dsl.DSLSchema) -> dict:
    '''
    Get Results for a specific event
    '''
    inner = [
        ds.Standing.placement,
        ds.Standing.entrant.select(
            ds.Entrant.id,
            ds.Entrant.participants.select(
                ds.Participant.gamerTag, ds.Participant.prefix, ds.Participant.user.select(
                    ds.User.authorizations(types=["TWITTER"]).select(ds.ProfileAuthorization.externalUsername)
                )
            )
        )
    ]

    standings = getAllEventStandings(event_id, inner, ds, session)
    return standings


def generateEventResults(event_id: int, standing_dict: dict, top_cut: int, ds, session) -> None:
    """
    Generate the printout for an event
    """
    standings = []
    for player in standing_dict:
        if player['placement'] <= top_cut:
            standings.append(player)
            getPlayerCharacterData(event_id, player['entrant']['id'], ds, session)

    standings = sorted(standings, key=lambda d: d['placement'])

    for s in standings:
        line = ""
        placement = s['placement']
        line += str(placement) + ". "
        entrant_id = s['entrant']['id']
        characters = None
        while characters is None:
            try:
                characters = getPlayerCharacterData(event_id, entrant_id, ds, session)
            except gql.transport.exceptions.TransportServerError as to:
                time.sleep(1.5)
        time.sleep(.3)
        for p in s['entrant']['participants']:
            prefix = p['prefix']
            tag = p['gamerTag']
            twitter = None
            if len(p['user']['authorizations']) > 0:
                twitter = p['user']['authorizations'][0]['externalUsername']
            if prefix:
                line += prefix + " | "
            line += tag
            if twitter:
                line += "(@" + twitter + ")"

        if len(characters) != 0:
            line += " - "
            for character in characters:
                line += character + ", "
            line = line[:-2]

        print(line)


def getPlayerCharacterData(event_id: int, entrant_id: int, ds, session) -> list:
    """
    Gets the characters a player has played if startgg has it.
    """
    query = dsl.dsl_gql(
        dsl.DSLQuery(
            ds.Query.event(id=event_id).select(
                ds.Event.videogame.select(
                    ds.Videogame.characters.select(
                        ds.Character.id, ds.Character.name
                    )
                ),
                ds.Event.sets(page=1, perPage=150, sortType="RECENT", filters={"entrantIds": [entrant_id]}).select(ds.SetConnection.nodes.select(
                    ds.Set.games.select(ds.Game.selections.select(
                        ds.GameSelection.entrant.select(ds.Entrant.id),
                        ds.GameSelection.selectionType,
                        ds.GameSelection.selectionValue
                    ))
                ))
            )
        )
    )

    result = execute(query, session)
    characters = result['event']['videogame']['characters']
    if not characters:
        return []
    char_dict = {}
    for char in characters:
        char_dict[char['id']] = char['name']

    games = []
    for s in result['event']['sets']['nodes']:
        if s['games'] is not None:
            for game in s['games']:
                games.append(game)

    player_characters = []
    for game in games:
        if not game['selections']:
            return []
        for selection in game['selections']:
            if selection['entrant']['id'] == entrant_id and selection['selectionType'] == 'CHARACTER':
                player_characters.append(char_dict[selection['selectionValue']])

    return (list(set(player_characters)))


def parseLink(link: str) -> tuple:
    """
    Parses a link from an argument to determine which type of link it is.
    Shortens the link to the tournament/event slug and returns the slug type and value
    """

    # checks if a link contains an event slug. event slugs also contain tournament slugs, so checks first
    # eg. start.gg/tournament/evo-2023/event/guilty-gear-strive
    event_link = re.search(r"tournament\/[a-zA-Z0-9\-]+\/event\/[a-zA-Z0-9\-]+", link)
    if event_link:
        span = event_link.span()
        return ("event_slug", link[span[0]:span[1]])

    # checks if a link contains a full tournament slug, eg. start.gg/tournament/evo-2023
    tournament_link = re.search(r"tournament\/[a-zA-Z0-9\-]+", link)
    if tournament_link:
        span = tournament_link.span()
        return ("tournament_full_slug", link[span[0]:span[1]])

    # checks if a link contains a shorthand tournament slug, eg. start.gg/evo
    shorthand_link = re.search(r"start\.gg\/[a-zA-Z0-9\-]+", link)
    if shorthand_link:
        span = shorthand_link.span()
        return ("shorthand_slug", link[span[0]:span[1]].split('/')[1])

    # if no matches, assume it is a bare tournament shorthand, eg. evo
    return ("shorthand_slug", link)


def main():

    msg = "Generate Tournament Previews of start.gg events"

    parser = argparse.ArgumentParser(description=msg)
    parser.add_argument(
        "startggLink", help="Link to a start.gg tournament or event")
    parser.add_argument('-n', '--places', default=16,
                        help="Number of top seeds to display (default 16)")
    args = parser.parse_args()

    # Select your transport with a defined url endpoint
    transport = RequestsHTTPTransport(url="https://api.start.gg/gql/alpha", headers=gen_headers())

    # Create a GraphQL client using the defined transport
    client = Client(transport=transport, fetch_schema_from_transport=True)

    with client as session:
        assert client.schema is not None
        ds = dsl.DSLSchema(client.schema)

        link_type, link = parseLink(args.startggLink)
        if link_type == "event_slug":
            event_id, game, tournament = get_event_id(session, link)
            print("<h3>"+tournament + " " + game + "</h3>")
            players = getEventResults(session, event_id, ds)
            generateEventResults(event_id, players, int(args.places), ds, session)

        else:
            event_dict = getEvents(session, link)
            for event in event_dict:
                print(event_dict[event][0] + " - " + event_dict[event][1])
                players = getEventResults(session, event, ds)
                generateEventResults(event, players, int(args.places), ds, session)
                time.sleep(2)


if __name__ == "__main__":
    main()
