from abc import ABC, abstractmethod


class AbstractCallback(ABC):
    """
    Interface that defines how callbacks must be specified.
    """

    @abstractmethod
    def __call__(self, epoch, step, performance_measures, context):
        """
        Called after every batch by the ModelTrainer.
        Parameters:
            epoch (int): current epoch number
            step (int): current batch number
            performance_measures (dict): losses and metrics based on a running average
            context (ModelTrainer): reference to the calling ModelTrainer, allows to access members
        """
        pass

    def close(self):
        """
        Handle cleanup work if necessary. Will be called at the end of the last epoch.
        """
        pass
