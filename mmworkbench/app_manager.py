# -*- coding: utf-8 -*-
"""
This module contains the application manager
"""
from __future__ import unicode_literals
from builtins import object

from .dialogue import DialogueManager
from .processor.nlp import NaturalLanguageProcessor

class ApplicationManager(object):
    '''This class provides the functionality to manage a workbench application.

    language parser models. It is decoupled from any specific server
    implementation (underlying server framework, endpoints, request/response
    signatures).
    '''
    def __init__(self, app_path):
        self.nlp = NaturalLanguageProcessor(app_path)
        self.dialogue_manager = DialogueManager()

    def load(self):
        """Loads all resources required to run the application."""
        self.nlp.load()

    def parse(self, request):
        """
        Args:
            request (dict): A dictionary containing the body of the request
                received
        """

        #apply nlp here
        return self.dialogue_manager.apply_handler(request)

    def add_dialogue_rule(self, name, handler, **kwargs):
        """Adds a dialogue rule for the dialogue manager.

        Args:
            name (str): The name of the dialogue state
            handler (function): The dialogue state handler function
            **kwargs (dict): A list of options which specify the dialogue rule
        """
        self.dialogue_manager.add_dialogue_rule(name, handler, **kwargs)
